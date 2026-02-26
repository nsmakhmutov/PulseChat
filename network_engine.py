import socket
import threading
import json
import time
import queue
import pygame
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
)

MAX_SILENT_RECONNECT_ATTEMPTS = 4
RECONNECT_DELAY = 3.0

# Устанавливаем точность системного таймера в 1 мс на Windows.
# Без этого time.sleep(0.001) может спать 10-15мс — аудио глитчи.
if platform.system() == "Windows":
    try:
        winmm = ctypes.WinDLL('winmm')
        winmm.timeBeginPeriod(1)
    except Exception:
        pass


class NetworkClient(QObject):
    connected           = pyqtSignal(dict)
    global_state_update = pyqtSignal(dict)
    error_occurred      = pyqtSignal(str)

    connection_lost     = pyqtSignal()
    connection_restored = pyqtSignal()
    reconnect_failed    = pyqtSignal()

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

        # Последний проигранный звук soundboard.
        # Используется для блокировки спама: новый звук не запустится,
        # пока текущий ещё играет.
        self._sb_sound: "pygame.mixer.Sound | None" = None

        # -------------------------------------------------------------------
        # Pacing-очередь для видео-пакетов (leaky bucket).
        # Пакеты кладёт send_video_packet(), дренирует video_pacing_loop().
        # maxsize=2000: ~2.7 сек буфера при 720p60 6Mbps (737 пакетов/сек).
        # Если очередь заполнена — старые пакеты дропаются (актуальность важнее).
        # -------------------------------------------------------------------
        self.video_pacing_queue = queue.Queue(maxsize=2000)

        self._init_sockets()

    # ------------------------------------------------------------------
    # Сокеты
    # ------------------------------------------------------------------
    def _init_sockets(self):
        try:
            self.tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_socket_bound = False
        except Exception as e:
            print(f"[Net] Socket init error: {e}")

    # ------------------------------------------------------------------
    # Soundboard
    # ------------------------------------------------------------------
    def play_soundboard_file(self, filename):
        """
        Воспроизвести soundboard-файл.

        Защита от спама: новый звук НЕ запускается, пока предыдущий ещё играет.
        Это предотвращает накопление звуков при частых кликах.

        Громкость: квадратичная кривая (slider/100)^2.
        Соответствует той же перцептивной шкале, что и системные уведомления:
          slider 40 (default) -> 0.16x (~-16 dB)
          slider 70           -> 0.49x (~-6 dB)
          slider 100          -> 1.00x (0 dB, максимум pygame)
        """
        try:
            # Anti-spam: блокируем, пока текущий звук ещё играет
            if self._sb_sound is not None and self._sb_sound.get_num_channels() > 0:
                print(f"[Net] Soundboard: пропущен {filename!r} — звук ещё играет")
                return

            path = resource_path(os.path.join("assets/panel", filename))
            if os.path.exists(path):
                sound = pygame.mixer.Sound(path)
                # Квадратичная кривая: (raw/100)^2 — совпадает с системными звуками
                raw = int(QSettings("MyVoiceChat", "GlobalSettings").value("soundboard_volume", 40)) / 100.0
                vol = raw ** 2
                sound.set_volume(vol)
                sound.play()
                self._sb_sound = sound  # сохраняем для проверки get_num_channels()
                print(f"[Net] Playing soundboard: {filename} (vol={vol:.3f})")
            else:
                print(f"[Net] Soundboard file not found: {path}")
        except Exception as e:
            print(f"[Net] Soundboard error: {e}")

    # ------------------------------------------------------------------
    # Подключение к серверу
    # ------------------------------------------------------------------
    def connect_to_server(self, ip, nick, avatar):
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

        # 8 MB буфер приёма: при 6Mbps видео ≈ 750KB/s → запас ~10 сек.
        try:
            self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
            print("[Net] UDP receive buffer set to 8MB")
        except Exception:
            pass

        self.send_json({"action": "login", "nick": self._nick, "avatar": self._avatar})
        self.running       = True
        self._is_connected = True

        threading.Thread(target=self.tcp_listen,          daemon=True).start()
        threading.Thread(target=self.udp_sender_loop,     daemon=True).start()
        threading.Thread(target=self.udp_keepalive_loop,  daemon=True).start()
        threading.Thread(target=self.udp_receive_loop,    daemon=True).start()
        threading.Thread(target=self.ping_loop,           daemon=True).start()
        threading.Thread(target=self.video_pacing_loop,   daemon=True).start()

        print("[Net] Connected to server")

    # ------------------------------------------------------------------
    # Переподключение
    # ------------------------------------------------------------------
    def _on_connection_lost(self):
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

    # ------------------------------------------------------------------
    # Видео — пакеты кладём в pacing-очередь.
    # Реальная отправка происходит в video_pacing_loop() с ограничением
    # скорости, чтобы избежать burst'ов, которые перегружают Radmin VPN
    # и роняют пинг на каналах с RTT 40-50 мс.
    # ------------------------------------------------------------------
    def send_video_packet(self, payload):
        if not self.server_addr or self.audio.my_uid == 0:
            return
        header = UDP_HEADER_STRUCT.pack(self.audio.my_uid, time.time(), 0, FLAG_VIDEO)
        packet = header + payload
        # Если очередь переполнена — дропаем самый старый пакет, берём новый.
        # Актуальный кадр важнее давно стоящего в очереди.
        if self.video_pacing_queue.full():
            try:
                self.video_pacing_queue.get_nowait()
            except queue.Empty:
                pass
        try:
            self.video_pacing_queue.put_nowait(packet)
        except queue.Full:
            pass

    # ------------------------------------------------------------------
    # Leaky bucket pacing для видео-пакетов.
    #
    # Проблема (до pacing):
    #   _fragment_and_send() отправлял 100-300 UDP-пакетов за <1 мс в одном
    #   burst'е (особенно IDR-кадры). На 10ms-канале (RadminVPN) это проходит,
    #   на 40-50ms-канале очередь отправки ядра переполняется → ВСЕ UDP пакеты
    #   (включая ping) встают в очередь → ping улетает до 5000 мс.
    #
    # Решение (leaky bucket):
    #   Пакеты отправляются с постоянным интервалом ~1.4 мс, не превышая
    #   VIDEO_PACING_RATE_BYTES_SEC. Burst'ы невозможны.
    #
    # Точность на Windows:
    #   timeBeginPeriod(1) уже вызван в этом файле → time.sleep() имеет
    #   разрешение ~1 мс. Для sub-millisecond интервалов используем
    #   perf_counter busy-wait с порогом 0.5 мс.
    # ------------------------------------------------------------------
    def video_pacing_loop(self):
        # Средний размер видео-пакета: MAX_VIDEO_PAYLOAD + UDP_HEADER(13) + VIDEO_HEADER(8)
        avg_packet_bytes = MAX_VIDEO_PAYLOAD + 21
        # Интервал между пакетами в секундах
        pacing_interval  = avg_packet_bytes / VIDEO_PACING_RATE_BYTES_SEC  # ~1.39 мс

        SLEEP_THRESHOLD = 0.0005  # 0.5 мс — ниже этого busy-wait точнее sleep()

        last_send_t = time.perf_counter()

        while self.running:
            try:
                packet = self.video_pacing_queue.get(timeout=0.05)
            except queue.Empty:
                continue

            if not self.server_addr:
                continue

            # Ждём нужный момент отправки
            target_t = last_send_t + pacing_interval
            now      = time.perf_counter()
            delta    = target_t - now

            if delta > SLEEP_THRESHOLD:
                time.sleep(delta - SLEEP_THRESHOLD)
                # Busy-wait оставшиеся <0.5 мс для точности
                while time.perf_counter() < target_t:
                    pass
            elif delta > 0:
                # Короткий busy-wait (< 0.5 мс)
                while time.perf_counter() < target_t:
                    pass

            try:
                self.udp_sock.sendto(packet, self.server_addr)
                self.packets_sent += 1
            except Exception as e:
                print(f"[Net] Pacing send error: {e}")

            last_send_t = time.perf_counter()

    # ------------------------------------------------------------------
    # Приём UDP-пакетов
    # ------------------------------------------------------------------
    def udp_receive_loop(self):
        while self.running:
            try:
                data, addr = self.udp_sock.recvfrom(BUFFER_SIZE)
                if len(data) < UDP_HEADER_SIZE:
                    continue

                uid, ts, seq, flags = UDP_HEADER_STRUCT.unpack(data[:UDP_HEADER_SIZE])

                if flags == 254:
                    # Pong — измеряем RTT
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
                    # Голосовой поток стрима — Mix Minus без DSP.
                    # Payload: [speaker_uid: 4 байта] + [opus].
                    # Свой голос отбрасываем — не слышим себя в стриме.
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
                    # Стрим-аудио (системный звук / виртуальный кабель)
                    is_loopback = bool(flags & FLAG_LOOPBACK_AUDIO)
                    if seq % 50 == 0:
                        print(f"[Net-Recv] FLAG_STREAM_AUDIO (loopback={is_loopback}) от uid={uid}")
                    self.audio.add_incoming_stream_packet(uid, seq, data[UDP_HEADER_SIZE:], flags)

                elif flags & FLAG_WHISPER:
                    # Шёпот — приватный голос от sender к нам.
                    # Payload: [target_uid: 4 байта] + [opus].
                    # Отбрасываем 4-байтовый заголовок target_uid перед декодированием,
                    # иначе opuslib получит мусор в начале и вернёт ошибку.
                    if len(data) < UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE:
                        continue
                    opus_payload = data[UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE:]
                    # add_incoming_whisper_packet: испускает сигнал whisper_received(uid)
                    # при первом пакете от нового шептуна → UI показывает баннер.
                    self.audio.add_incoming_whisper_packet(uid, seq, opus_payload)

                else:
                    # Обычный голос чата
                    self.audio.add_incoming_packet(uid, seq, data[UDP_HEADER_SIZE:], flags)

            except Exception as e:
                if self.running:
                    print(f"[Net] UDP receive error: {e}")
                continue

    # ------------------------------------------------------------------
    # Отправка аудио-пакетов из очереди AudioHandler
    # ------------------------------------------------------------------
    def udp_sender_loop(self):
        while self.running:
            try:
                packet = self.audio.send_queue.get(timeout=0.1)
                if self.server_addr:
                    self.udp_sock.sendto(packet, self.server_addr)
            except Exception:
                continue

    # ------------------------------------------------------------------
    # Keepalive (статус mute/deaf) и Ping
    # ------------------------------------------------------------------
    def udp_keepalive_loop(self):
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
        while self.running:
            if self.audio.my_uid != 0:
                try:
                    header = UDP_HEADER_STRUCT.pack(self.audio.my_uid, time.time(), 0, 254)
                    self.udp_sock.sendto(header, self.server_addr)
                    self.packets_sent += 1
                except Exception as e:
                    print(f"[Net] Ping error: {e}")
            time.sleep(7)

    # ------------------------------------------------------------------
    # TCP — команды сервера
    # ------------------------------------------------------------------
    def tcp_listen(self):
        raw_data = ""
        # JSONDecoder создаём ОДИН РАЗ — он stateless и thread-safe.
        # Создание внутри цикла (старый код) аллоцировало новый объект на КАЖДОЕ
        # входящее сообщение: при 10 sync/сек это +10 аллокаций/сек без причины.
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

    def process_message(self, msg):
        act = msg.get('action')
        if act == 'login_success':
            self.connected.emit(msg)
            print(f"[Net] Login success, UID: {msg.get('uid')}")
        elif act == 'sync_users':
            self.global_state_update.emit(msg.get('all_users', {}))
        elif act == 'play_soundboard':
            self.play_soundboard_file(msg.get('file'))
        elif act == 'request_keyframe':
            if self.video:
                self.video.force_keyframe()
                print("[Net] IDR keyframe запрошен сервером → передано VideoEngine")

    def send_json(self, data):
        try:
            self.tcp_sock.sendall(json.dumps(data).encode('utf-8'))
        except Exception as e:
            print(f"[Net] Send JSON error: {e}")

    def update_user_info(self, nick, avatar):
        self.send_json({"action": "update_user", "nick": nick, "avatar": avatar})

    def send_status_update(self, mute, deaf):
        self.send_json({"action": "update_status", "mute": mute, "deaf": deaf})

    def set_video_engine(self, video):
        self.video = video
        print("[Net] VideoEngine registered")

    # ------------------------------------------------------------------
    # Заглушки для совместимости с ui_main.py (качество не реализовано).
    # Кнопка качества в оверлее работает визуально, но на маршрутизацию
    # сервера не влияет — все зрители получают полный поток.
    # ------------------------------------------------------------------
    def send_quality_request(self, skip_factor: int):
        """Stub: в текущей архитектуре качество не маршрутизируется."""
        pass

    def request_viewer_keyframe(self, streamer_uid: int):
        """Stub: IDR-таймер из ui_video.py вызывает этот метод периодически."""
        pass