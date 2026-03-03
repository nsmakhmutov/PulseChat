import socket
import threading
import json
import time
import queue
import io
import base64
import sounddevice as sd
import soundfile as sf
import numpy as np
import struct
import os
import platform
import ctypes

from PyQt6.QtCore import QObject, pyqtSignal, QSettings

from config import (
    resource_path, DEFAULT_PORT_TCP, DEFAULT_PORT_UDP, BUFFER_SIZE,
    UDP_HEADER_STRUCT, UDP_HEADER_SIZE, FLAG_VIDEO, FLAG_STREAM_AUDIO, MAX_VIDEO_PAYLOAD,
    CMD_SOUNDBOARD, FLAG_LOOPBACK_AUDIO, FLAG_STREAM_VOICES,
    STREAM_VOICE_HEADER_STRUCT, STREAM_VOICE_HEADER_SIZE,
    VIDEO_PACING_RATE_BYTES_SEC, FLAG_WHISPER,
    CMD_NUDGE_VOTE, CMD_PLAY_NUDGE, CMD_NUDGE_TRIGGERED, NUDGE_SOUND_PATH,
)

MAX_SILENT_RECONNECT_ATTEMPTS = 4
RECONNECT_DELAY = 3.0

# Точность системного таймера 1 мс на Windows: без этого time.sleep(0.001) спит 10–15 мс
if platform.system() == "Windows":
    try:
        ctypes.WinDLL('winmm').timeBeginPeriod(1)
    except Exception:
        pass


class NetworkClient(QObject):
    """Сетевой клиент: TCP-команды, UDP-аудио/видео, reconnect, soundboard, nudge."""

    connected           = pyqtSignal(dict)
    global_state_update = pyqtSignal(dict)
    error_occurred      = pyqtSignal(str)

    connection_lost     = pyqtSignal()
    connection_restored = pyqtSignal()
    reconnect_failed    = pyqtSignal()

    soundboard_played   = pyqtSignal(str)   # (from_nick)

    nudge_received  = pyqtSignal()
    nudge_triggered = pyqtSignal(str, str)  # (target_nick, voter_nick)

    def __init__(self, audio):
        super().__init__()
        self.audio  = audio
        self.video  = None
        self.server_addr  = None
        self.running      = False
        self.current_ping = 0
        self.packets_sent = 0
        self.packets_received = 0

        self._ip     = None
        self._nick   = None
        self._avatar = None

        self._is_connected       = False
        self._reconnecting       = False
        self._reconnect_attempts = 0

        # Блокировка спама soundboard: новый звук не запустится пока играет текущий
        self._sb_playing = threading.Event()

        # Pacing-очередь для видео (leaky bucket).
        # maxsize=2000: ~2.7 сек буфера при 720p60 6Mbps (737 пакетов/сек).
        self.video_pacing_queue = queue.Queue(maxsize=2000)

        self._init_sockets()

    # ── Сокеты ───────────────────────────────────────────────────────────────

    def _init_sockets(self):
        """Создаёт новые TCP/UDP-сокеты, закрывая предыдущие если они есть."""
        for attr in ('tcp_sock', 'udp_sock'):
            old = getattr(self, attr, None)
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
        try:
            self.tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_socket_bound = False
        except Exception as e:
            print(f"[Net] Socket init error: {e}")

    # ── Soundboard ────────────────────────────────────────────────────────────

    def play_soundboard_file(self, filename: str, data_b64: str = None,
                             from_nick: str = None):
        """Воспроизводит soundboard-файл через sounddevice.

        Режим 1 (data_b64 is None): файл ищется в assets/panel/ по имени.
        Режим 2 (data_b64 задан): аудио декодируется из base64 прямо в памяти.
        Новый звук пропускается если предыдущий ещё играет (anti-spam).

            :param filename: имя файла или метка '__custom__:...'
            :param data_b64: base64-данные кастомного звука; None — стандартный файл
            :param from_nick: ник отправителя для сигнала soundboard_played
        """
        try:
            if self._sb_playing.is_set():
                print(f"[Net] Soundboard: пропущен {filename!r} — звук ещё играет")
                return

            raw = int(QSettings("MyVoiceChat", "GlobalSettings").value("soundboard_volume", 40)) / 100.0
            vol = raw ** 2  # квадратичная кривая громкости

            if data_b64:
                try:
                    audio_bytes = base64.b64decode(data_b64)
                    audio_source = io.BytesIO(audio_bytes)
                except Exception as e:
                    print(f"[Net] Soundboard base64 decode error: {e}")
                    return
            else:
                if filename and filename.startswith("__custom__:"):
                    print("[Net] Soundboard: кастомный звук без data_b64 — пропущен")
                    return
                path = resource_path(os.path.join("assets/panel", filename))
                if not os.path.exists(path):
                    print(f"[Net] Soundboard file not found: {path}")
                    return
                audio_source = path

            if from_nick:
                self.soundboard_played.emit(from_nick)

            def _play():
                try:
                    self._sb_playing.set()
                    data, sr = sf.read(audio_source, dtype='float32')
                    sd.play(data * vol, sr)
                    sd.wait()
                except Exception as e:
                    print(f"[Net] Soundboard playback error: {e}")
                finally:
                    self._sb_playing.clear()

            threading.Thread(target=_play, daemon=True, name="soundboard-play").start()
            print(f"[Net] Playing soundboard: {filename} "
                  f"(vol={vol:.3f}, custom={bool(data_b64)}, by={from_nick!r})")
        except Exception as e:
            print(f"[Net] Soundboard error: {e}")

    # ── Подключение к серверу ────────────────────────────────────────────────

    def connect_to_server(self, ip: str, nick: str, avatar: str):
        """Запускает первичное подключение в фоновом потоке.

            :param ip: IP-адрес сервера
            :param nick: никнейм пользователя
            :param avatar: имя файла аватарки
        """
        self._ip     = ip
        self._nick   = nick
        self._avatar = avatar
        self._reconnect_attempts = 0
        self._reconnecting = False
        threading.Thread(target=self._connect_initial, daemon=True).start()

    def _connect_initial(self):
        try:
            self._do_connect()
        except Exception as e:
            print(f"[Net] Initial connection failed: {e}")
            self._reconnecting = True
            self._reconnect_attempts = 0
            self.connection_lost.emit()
            self._reconnect_loop()

    def _do_connect(self):
        """Выполняет TCP/UDP подключение и запускает рабочие потоки."""
        self.server_addr = (self._ip, DEFAULT_PORT_UDP)

        self.tcp_sock.settimeout(5.0)
        self.tcp_sock.connect((self._ip, DEFAULT_PORT_TCP))
        self.tcp_sock.settimeout(None)

        if not self.udp_socket_bound:
            try:
                self.udp_sock.bind(('0.0.0.0', 0))
                self.udp_socket_bound = True
                local_port = self.udp_sock.getsockname()[1]
                print(f"[Net] UDP socket bound to local port {local_port}")
            except Exception as e:
                print(f"[Net] CRITICAL: UDP bind failed: {e}")
                raise

        try:
            self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
            print("[Net] UDP receive buffer set to 8MB")
        except Exception:
            pass

        self.send_json({"action": "login", "nick": self._nick, "avatar": self._avatar})
        self.running       = True
        self._is_connected = True

        threading.Thread(target=self.tcp_listen,         daemon=True).start()
        threading.Thread(target=self.udp_sender_loop,    daemon=True).start()
        threading.Thread(target=self.udp_keepalive_loop, daemon=True).start()
        threading.Thread(target=self.udp_receive_loop,   daemon=True).start()
        threading.Thread(target=self.ping_loop,          daemon=True).start()
        threading.Thread(target=self.video_pacing_loop,  daemon=True).start()

        print("[Net] Connected to server")

    # ── Переподключение ───────────────────────────────────────────────────────

    def _on_connection_lost(self):
        """Запускает цикл переподключения при разрыве соединения."""
        if self._reconnecting:
            return
        self._reconnecting       = True
        self._is_connected       = False
        self.running             = False
        self._reconnect_attempts = 0

        print("[Net] Connection lost. Starting reconnect loop...")
        self.connection_lost.emit()
        threading.Thread(target=self._reconnect_loop, daemon=True).start()

    def _reconnect_loop(self):
        """Делает MAX_SILENT_RECONNECT_ATTEMPTS попыток переподключения с паузой."""
        while self._reconnect_attempts < MAX_SILENT_RECONNECT_ATTEMPTS:
            self._reconnect_attempts += 1
            print(f"[Net] Reconnect attempt {self._reconnect_attempts}/{MAX_SILENT_RECONNECT_ATTEMPTS}...")
            time.sleep(RECONNECT_DELAY)
            try:
                self._init_sockets()
                self._do_connect()
                self._reconnecting       = False
                self._reconnect_attempts = 0
                print("[Net] Reconnected successfully!")
                self.connection_restored.emit()
                return
            except Exception as e:
                print(f"[Net] Reconnect attempt {self._reconnect_attempts} failed: {e}")

        print("[Net] All reconnect attempts failed. Notifying user.")
        self._reconnecting = False
        self.reconnect_failed.emit()

    def manual_reconnect(self):
        """Запускает ручное переподключение по запросу пользователя."""
        if self._reconnecting:
            print("[Net] Already reconnecting...")
            return
        if not self._ip:
            print("[Net] No server address saved, cannot reconnect.")
            return
        print("[Net] Manual reconnect requested.")
        self._reconnecting       = True
        self._reconnect_attempts = 0
        self.running = False
        threading.Thread(target=self._reconnect_loop, daemon=True).start()

    # ── Видео — pacing-очередь ────────────────────────────────────────────────

    def send_video_packet(self, payload: bytes):
        """Кладёт видео-пакет в pacing-очередь; переполнение → дроп старого.

            :param payload: сырые байты видео-чанка
        """
        if not self.server_addr or self.audio.my_uid == 0:
            return
        header = UDP_HEADER_STRUCT.pack(self.audio.my_uid, time.time(), 0, FLAG_VIDEO)
        packet = header + payload
        if self.video_pacing_queue.full():
            try:
                self.video_pacing_queue.get_nowait()
            except queue.Empty:
                pass
        try:
            self.video_pacing_queue.put_nowait(packet)
        except queue.Full:
            pass

    def video_pacing_loop(self):
        """Leaky bucket: отправляет видео-пакеты с постоянным интервалом.

        Устраняет burst'ы, из-за которых на каналах с RTT 40–50 мс (RadminVPN)
        очередь ядра переполняется и ping улетает до 5000 мс.
        Sub-ms точность: sleep до порога, затем busy-wait остаток.
        """
        avg_packet_bytes = MAX_VIDEO_PAYLOAD + 21   # payload + UDP(13) + VIDEO(8)
        pacing_interval  = avg_packet_bytes / VIDEO_PACING_RATE_BYTES_SEC  # ~1.39 мс
        SLEEP_THRESHOLD  = 0.0003  # 0.3 мс: граница перехода sleep → busy-wait

        last_send_t = time.perf_counter()

        while self.running:
            try:
                packet = self.video_pacing_queue.get(timeout=0.05)
            except queue.Empty:
                continue

            if not self.server_addr:
                continue

            target_t = last_send_t + pacing_interval
            now = time.perf_counter()
            delta = target_t - now

            if delta > 0:
                # В Windows с timeBeginPeriod(1) sleep работает с точностью ~1-2 мс
                time.sleep(delta)

            try:
                self.udp_sock.sendto(packet, self.server_addr)
                self.packets_sent += 1
            except Exception as e:
                pass  # Убрал печать, чтобы не спамить в консоль при разрывах

            last_send_t = time.perf_counter()

    # ── Приём UDP-пакетов ─────────────────────────────────────────────────────

    def udp_receive_loop(self):
        """Принимает и маршрутизирует входящие UDP-пакеты."""
        while self.running:
            try:
                data, addr = self.udp_sock.recvfrom(BUFFER_SIZE)
                if len(data) < UDP_HEADER_SIZE:
                    continue

                uid, ts, seq, flags = UDP_HEADER_STRUCT.unpack(data[:UDP_HEADER_SIZE])

                if flags == 254:
                    # Pong: обновляем RTT (EWMA 0.7/0.3)
                    self.packets_received += 1
                    delay = (time.time() - ts) * 1000
                    if self.current_ping == 0:
                        self.current_ping = int(delay)
                    else:
                        self.current_ping = int(self.current_ping * 0.7 + delay * 0.3)

                elif flags & FLAG_VIDEO:
                    if self.video:
                        self.video.process_incoming_packet(uid, data[UDP_HEADER_SIZE:])
                    else:
                        print(f"[Net] Video packet from {uid}, but VideoEngine not initialized")

                elif flags & FLAG_STREAM_AUDIO and flags & FLAG_STREAM_VOICES:
                    # Mix Minus: payload [speaker_uid: 4 байта] + [opus]
                    # Свой голос отбрасываем — не слышим себя в стриме
                    if len(data) < UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE:
                        continue
                    speaker_uid, = STREAM_VOICE_HEADER_STRUCT.unpack(
                        data[UDP_HEADER_SIZE: UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE]
                    )
                    if speaker_uid == self.audio.my_uid:
                        continue
                    opus_payload = data[UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE:]
                    self.audio.add_incoming_stream_packet(speaker_uid, seq, opus_payload, flags)

                elif flags & FLAG_STREAM_AUDIO:
                    is_loopback = bool(flags & FLAG_LOOPBACK_AUDIO)
                    if seq % 50 == 0:
                        print(f"[Net-Recv] FLAG_STREAM_AUDIO (loopback={is_loopback}) от uid={uid}")
                    self.audio.add_incoming_stream_packet(uid, seq, data[UDP_HEADER_SIZE:], flags)

                elif flags & FLAG_WHISPER:
                    # Payload: [target_uid: 4 байта] + [opus]
                    # Отбрасываем 4-байтовый заголовок перед передачей в декодер
                    if len(data) < UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE:
                        continue
                    opus_payload = data[UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE:]
                    self.audio.add_incoming_whisper_packet(uid, seq, opus_payload)

                else:
                    self.audio.add_incoming_packet(uid, seq, data[UDP_HEADER_SIZE:], flags)

            except Exception as e:
                if self.running:
                    print(f"[Net] UDP receive error: {e}")
                continue

    # ── Отправка аудио-пакетов ────────────────────────────────────────────────

    def udp_sender_loop(self):
        """Дренирует очередь send_queue AudioHandler и отправляет UDP-пакеты."""
        while self.running:
            try:
                packet = self.audio.send_queue.get(timeout=0.1)
                if self.server_addr:
                    self.udp_sock.sendto(packet, self.server_addr)
            except Exception:
                continue

    # ── Keepalive и Ping ──────────────────────────────────────────────────────

    def udp_keepalive_loop(self):
        """Раз в секунду отправляет UDP-пакет с текущими флагами mute/deaf."""
        while self.running:
            if self.audio.my_uid != 0:
                flags = (1 if self.audio.is_muted else 0) | (2 if self.audio.is_deafened else 0)
                try:
                    header = UDP_HEADER_STRUCT.pack(self.audio.my_uid, time.time(), 0, flags)
                    self.udp_sock.sendto(header, self.server_addr)
                except Exception as e:
                    print(f"[Net] Keepalive error: {e}")
            time.sleep(1)

    def ping_loop(self):
        """Раз в 7 секунд отправляет ping-пакет (flags=254) для измерения RTT."""
        while self.running:
            if self.audio.my_uid != 0:
                try:
                    header = UDP_HEADER_STRUCT.pack(self.audio.my_uid, time.time(), 0, 254)
                    self.udp_sock.sendto(header, self.server_addr)
                    self.packets_sent += 1
                except Exception as e:
                    print(f"[Net] Ping error: {e}")
            time.sleep(7)

    # ── TCP — команды сервера ─────────────────────────────────────────────────

    def tcp_listen(self):
        """Принимает TCP-сообщения от сервера и передаёт в process_message."""
        raw_data = ""
        # JSONDecoder создаётся один раз — stateless, избегаем аллокаций в цикле
        _decoder = json.JSONDecoder()
        while self.running:
            try:
                chunk_bytes = self.tcp_sock.recv(4096)
                if not chunk_bytes:
                    print("[Net] Server closed connection (empty recv).")
                    break
                raw_data += chunk_bytes.decode('utf-8', errors='ignore')
                while True:
                    try:
                        msg, idx = _decoder.raw_decode(raw_data)
                        raw_data = raw_data[idx:].lstrip()
                        self.process_message(msg)
                    except json.JSONDecodeError:
                        break
            except (ConnectionResetError, ConnectionAbortedError, OSError) as e:
                if self.running:
                    print(f"[Net] TCP connection error: {e}")
                break
            except Exception as e:
                if self.running:
                    print(f"[Net] TCP receive error: {e}")
                break

        print("[Net] TCP listener stopped")
        if self.running:
            self._on_connection_lost()

    def process_message(self, msg: dict):
        """Диспетчер входящих TCP-команд от сервера.

            :param msg: десериализованный JSON-словарь
        """
        act = msg.get('action')
        if act == 'login_success':
            self.connected.emit(msg)
            print(f"[Net] Login success, UID: {msg.get('uid')}")
        elif act == 'sync_users':
            self.global_state_update.emit(msg.get('all_users', {}))
        elif act == 'play_soundboard':
            self.play_soundboard_file(msg.get('file'), msg.get('data_b64'), msg.get('from_nick'))
        elif act == 'request_keyframe':
            if self.video:
                self.video.force_keyframe()
                print("[Net] IDR keyframe запрошен сервером → передано VideoEngine")
        elif act == CMD_PLAY_NUDGE:
            # Нас пнули: звук обходит deaf/mute — цель фичи «достучаться» до АФК
            threading.Thread(
                target=self._play_nudge_sound,
                daemon=True,
                name="nudge-sound",
            ).start()
            self.nudge_received.emit()
        elif act == CMD_NUDGE_TRIGGERED:
            target_nick = msg.get('target_nick', '?')
            voter_nick  = msg.get('voter_nick',  '?')
            self.nudge_triggered.emit(target_nick, voter_nick)

    def send_json(self, data: dict):
        """Отправляет JSON-команду серверу по TCP.

            :param data: словарь для сериализации
        """
        try:
            self.tcp_sock.sendall(json.dumps(data).encode('utf-8'))
        except Exception as e:
            print(f"[Net] Send JSON error: {e}")

    def update_user_info(self, nick: str, avatar: str):
        """Отправляет обновление никнейма и аватарки на сервер.

            :param nick: новый никнейм
            :param avatar: имя файла аватарки
        """
        self.send_json({"action": "update_user", "nick": nick, "avatar": avatar})

    def send_status_update(self, mute: bool, deaf: bool):
        """Отправляет текущие флаги mute/deaf на сервер.

            :param mute: состояние заглушения микрофона
            :param deaf: состояние заглушения воспроизведения
        """
        self.send_json({"action": "update_status", "mute": mute, "deaf": deaf})

    def send_presence_update(self, status_icon: str, status_text: str):
        """Отправляет новый «статус дела» пользователя на сервер.

            :param status_icon: имя SVG-файла из assets/status/ или '' — убрать статус
            :param status_text: подпись ≤ 30 символов или ''
        """
        self.send_json({
            "action":      "update_presence",
            "status_icon": status_icon,
            "status_text": status_text,
        })

    def set_video_engine(self, video):
        """Регистрирует VideoEngine для маршрутизации видео-пакетов.

            :param video: экземпляр VideoEngine
        """
        self.video = video
        print("[Net] VideoEngine registered")

    # ── Фича «Пнуть» (Nudge) ─────────────────────────────────────────────────

    def _nudge_get_endpoint_vol(self):
        """Возвращает IAudioEndpointVolume дефолтного устройства воспроизведения.

        Поддерживает pycaw < 0.6 (GetSpeakers → IMMDevice напрямую)
        и pycaw >= 0.6 (GetSpeakers → AudioDevice-обёртка, IMMDevice в ._dev).
        Fallback — comtypes напрямую без pycaw.

            :return: IAudioEndpointVolume* или None при ошибке
        """
        # Попытка 1: pycaw
        try:
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            from comtypes import CLSCTX_ALL
            from ctypes import cast, POINTER

            device = AudioUtilities.GetSpeakers()
            if hasattr(device, 'Activate'):
                raw_dev = device          # pycaw < 0.6: уже IMMDevice
            elif hasattr(device, '_dev'):
                raw_dev = device._dev     # pycaw >= 0.6: IMMDevice внутри обёртки
            else:
                raise RuntimeError(f"Неизвестный тип GetSpeakers(): {type(device).__name__}")

            iface = raw_dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            return cast(iface, POINTER(IAudioEndpointVolume))

        except ImportError:
            print("[Nudge] pycaw не установлен — пробуем comtypes напрямую")
        except Exception as e:
            print(f"[Nudge] pycaw get_endpoint_vol error: {e}")

        # Попытка 2: comtypes напрямую
        try:
            import comtypes
            import comtypes.client
            from ctypes import cast, POINTER, c_float, c_int, c_uint, HRESULT

            CLSID_MMDeviceEnumerator = comtypes.GUID('{BCDE0395-E52F-467C-8E3D-C4579291692E}')
            IID_IMMDeviceEnumerator  = comtypes.GUID('{A95664D2-9614-4F35-A746-DE8DB63617E6}')
            IID_IMMDevice            = comtypes.GUID('{D666063F-1587-4E43-81F1-B948E807363F}')
            IID_IAudioEndpointVolume = comtypes.GUID('{5CDF2C82-841E-4546-9722-0CF74078229A}')

            class IMMDevice(comtypes.IUnknown):
                _iid_    = IID_IMMDevice
                _methods_ = [
                    comtypes.COMMETHOD(
                        [], HRESULT, 'Activate',
                        (['in'],  comtypes.GUID,              'iid'),
                        (['in'],  c_uint,                     'dwClsCtx'),
                        (['in'],  comtypes.c_void_p,          'pActivationParams'),
                        (['out'], POINTER(comtypes.c_void_p), 'ppInterface'),
                    ),
                    comtypes.COMMETHOD([], HRESULT, 'OpenPropertyStore',
                        (['in'],  c_uint, 'stgmAccess'),
                        (['out'], POINTER(comtypes.IUnknown), 'ppProperties'),
                    ),
                    comtypes.COMMETHOD([], HRESULT, 'GetId',
                        (['out'], POINTER(comtypes.c_wchar_p), 'ppstrId'),
                    ),
                    comtypes.COMMETHOD([], HRESULT, 'GetState',
                        (['out'], POINTER(c_uint), 'pdwState'),
                    ),
                ]

            class IMMDeviceEnumerator(comtypes.IUnknown):
                _iid_    = IID_IMMDeviceEnumerator
                _methods_ = [
                    comtypes.COMMETHOD(
                        [], HRESULT, 'EnumAudioEndpoints',
                        (['in'],  c_uint, 'dataFlow'),
                        (['in'],  c_uint, 'dwStateMask'),
                        (['out'], POINTER(comtypes.IUnknown), 'ppDevices'),
                    ),
                    comtypes.COMMETHOD(
                        [], HRESULT, 'GetDefaultAudioEndpoint',
                        (['in'],  c_uint,             'dataFlow'),
                        (['in'],  c_uint,             'role'),
                        (['out'], POINTER(IMMDevice), 'ppEndpoint'),
                    ),
                ]

            class IAudioEndpointVolumeDirect(comtypes.IUnknown):
                _iid_    = IID_IAudioEndpointVolume
                _methods_ = [
                    comtypes.COMMETHOD([], HRESULT, 'RegisterControlChangeNotify',
                        (['in'], comtypes.IUnknown, 'pNotify')),
                    comtypes.COMMETHOD([], HRESULT, 'UnregisterControlChangeNotify',
                        (['in'], comtypes.IUnknown, 'pNotify')),
                    comtypes.COMMETHOD([], HRESULT, 'GetChannelCount',
                        (['out'], POINTER(c_uint), 'pnChannelCount')),
                    comtypes.COMMETHOD([], HRESULT, 'SetMasterVolumeLevel',
                        (['in'], c_float, 'fLevelDB'),
                        (['in'], comtypes.c_void_p, 'pguidEventContext')),
                    comtypes.COMMETHOD([], HRESULT, 'SetMasterVolumeLevelScalar',
                        (['in'], c_float, 'fLevel'),
                        (['in'], comtypes.c_void_p, 'pguidEventContext')),
                    comtypes.COMMETHOD([], HRESULT, 'GetMasterVolumeLevel',
                        (['out'], POINTER(c_float), 'pfLevelDB')),
                    comtypes.COMMETHOD([], HRESULT, 'GetMasterVolumeLevelScalar',
                        (['out'], POINTER(c_float), 'pfLevel')),
                    comtypes.COMMETHOD([], HRESULT, 'SetChannelVolumeLevel',
                        (['in'], c_uint,  'nChannel'),
                        (['in'], c_float, 'fLevelDB'),
                        (['in'], comtypes.c_void_p, 'pguidEventContext')),
                    comtypes.COMMETHOD([], HRESULT, 'SetChannelVolumeLevelScalar',
                        (['in'], c_uint,  'nChannel'),
                        (['in'], c_float, 'fLevel'),
                        (['in'], comtypes.c_void_p, 'pguidEventContext')),
                    comtypes.COMMETHOD([], HRESULT, 'GetChannelVolumeLevel',
                        (['in'],  c_uint, 'nChannel'),
                        (['out'], POINTER(c_float), 'pfLevelDB')),
                    comtypes.COMMETHOD([], HRESULT, 'GetChannelVolumeLevelScalar',
                        (['in'],  c_uint, 'nChannel'),
                        (['out'], POINTER(c_float), 'pfLevel')),
                    comtypes.COMMETHOD([], HRESULT, 'SetMute',
                        (['in'], c_int, 'bMute'),
                        (['in'], comtypes.c_void_p, 'pguidEventContext')),
                    comtypes.COMMETHOD([], HRESULT, 'GetMute',
                        (['out'], POINTER(c_int), 'pbMute')),
                ]

            comtypes.CoInitialize()
            enumerator = comtypes.client.CreateObject(
                CLSID_MMDeviceEnumerator,
                interface=IMMDeviceEnumerator,
            )
            # eRender=0, eConsole=0 → дефолтное устройство воспроизведения
            device = enumerator.GetDefaultAudioEndpoint(0, 0)
            iface  = device.Activate(IID_IAudioEndpointVolume, 0x17, None)
            return cast(iface, POINTER(IAudioEndpointVolumeDirect))

        except Exception as e:
            print(f"[Nudge] comtypes direct error: {e}")

        return None

    def _nudge_boost_volume(self) -> tuple:
        """Снимает системный мьют и поднимает громкость если она ниже 30%.

            :return: (prev_scalar, was_muted) для последующего восстановления
        """
        NUDGE_MIN_VOL   = 0.30
        NUDGE_BOOST_VOL = 0.80

        prev_scalar = -1.0
        was_muted   = False

        vol = self._nudge_get_endpoint_vol()
        if vol is None:
            print("[Nudge] IAudioEndpointVolume недоступен — громкость не изменена")
            return prev_scalar, was_muted

        try:
            prev_scalar = float(vol.GetMasterVolumeLevelScalar())
            was_muted   = bool(vol.GetMute())

            if was_muted:
                vol.SetMute(False, None)
                print("[Nudge] Системный мьют снят")

            if prev_scalar < NUDGE_MIN_VOL:
                vol.SetMasterVolumeLevelScalar(NUDGE_BOOST_VOL, None)
                print(f"[Nudge] Громкость {prev_scalar:.0%} → {NUDGE_BOOST_VOL:.0%}")

        except Exception as e:
            print(f"[Nudge] boost error: {e}")

        return prev_scalar, was_muted

    def _nudge_restore_volume(self, prev_scalar: float, was_muted: bool):
        """Восстанавливает мастер-громкость и мьют после воспроизведения.

            :param prev_scalar: предыдущее значение громкости (−1 = не читалось)
            :param was_muted: было ли устройство замьючено
        """
        if prev_scalar < 0:
            return

        vol = self._nudge_get_endpoint_vol()
        if vol is None:
            return

        try:
            vol.SetMasterVolumeLevelScalar(prev_scalar, None)
            if was_muted:
                vol.SetMute(True, None)
            print(f"[Nudge] Громкость восстановлена → {prev_scalar:.0%}"
                  + (" + мьют" if was_muted else ""))
        except Exception as e:
            print(f"[Nudge] restore error: {e}")

    def _play_nudge_sound(self):
        """Воспроизводит Danger.mp3 + системный писк независимо от deaf/mute.

        Порядок: системный warning-звук → тональный Beep(1200, 400) → Danger.mp3.
        Перед воспроизведением форсирует громкость ≥ 30%, после — восстанавливает.
        """
        import winsound as _ws

        prev_scalar, was_muted = self._nudge_boost_volume()

        try:
            try:
                _ws.MessageBeep(0x30)   # MB_ICONEXCLAMATION
            except Exception as e:
                print(f"[Nudge] MessageBeep error: {e}")

            try:
                _ws.Beep(1200, 400)
            except Exception as e:
                print(f"[Nudge] Beep error: {e}")

            sound_path = NUDGE_SOUND_PATH if os.path.exists(NUDGE_SOUND_PATH) else None
            if sound_path is None:
                print(f"[Nudge] Danger.mp3 не найден: {NUDGE_SOUND_PATH}")
                return

            try:
                data, sr = sf.read(sound_path, dtype='float32')
                sd.play(data, sr)
                sd.wait()
                print("[Nudge] Danger.mp3 воспроизведён успешно")
            except Exception as e:
                print(f"[Nudge] playback error: {e}")

        finally:
            self._nudge_restore_volume(prev_scalar, was_muted)

    def send_nudge_vote(self, target_uid: int):
        """Отправляет серверу голос «Пнуть» для указанного пользователя.

            :param target_uid: uid пользователя-цели
        """
        self.send_json({'action': CMD_NUDGE_VOTE, 'target_uid': target_uid})
        print(f"[Net] Nudge vote sent → target_uid={target_uid}")

    # ── Stubs (качество не реализовано) ──────────────────────────────────────

    def send_quality_request(self, skip_factor: int):
        """Stub: маршрутизация по качеству не реализована."""
        pass

    def request_viewer_keyframe(self, streamer_uid: int):
        """Stub: IDR-таймер из ui_video.py вызывает этот метод периодически."""
        pass