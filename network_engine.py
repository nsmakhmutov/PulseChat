# network_engine.py — сетевой клиент InPulse (UDP голос + TCP + WebRTC сигнализация)
#
# ─── Архитектура после рефакторинга ────────────────────────────────────────────
#
#   NetworkClient — монолит: управляет всеми сетевыми соединениями.
#
#   НЕИЗМЕННЫЕ КОМПОНЕНТЫ (голос, soundboard, nudge, файлы):
#     tcp_sock / udp_sock     — существующие сокеты
#     tcp_listen / process_message  — TCP-приём
#     udp_sender_loop         — отправка голоса
#     udp_receive_loop        — приём голоса, whisper, ping (видео УДАЛЕНО)
#     udp_keepalive_loop      — статус mute/deaf
#     ping_loop               — EWMA RTT
#     play_soundboard_file    — soundboard
#     nudge методы            — nudge система
#     file_offer методы       — файлы P2P
#
#   НОВЫЕ КОМПОНЕНТЫ (WebRTC сигнализация):
#     _webrtc_loop            — asyncio event loop в daemon-потоке
#     _streamer_pc            — RTCPeerConnection стримера (если стримит)
#     _viewer_pc              — RTCPeerConnection зрителя (если смотрит)
#     _start_webrtc_loop()    — запуск asyncio loop при подключении
#     start_streaming_webrtc() — создать offer стримера, добавить DXCamTrack
#     stop_streaming_webrtc()  — закрыть PC стримера
#     _handle_viewer_offer_coro() — принять offer от SFU, создать answer
#     send_webrtc_offer/answer/ice — отправить WebRTC signaling через TCP
#
# ─── Что УДАЛЕНО по сравнению с предыдущей версией ────────────────────────────
#
#   video_pacing_queue          — UDP-pacing для видеочанков
#   send_video_frame_chunks()   — UDP-отправка видеочанков
#   send_video_packet()         — compat wrapper
#   video_pacing_loop()         — leaky bucket (заменён WebRTC)
#   FLAG_VIDEO ветка в udp_receive_loop — видео теперь через WebRTC
#   FLAG_STREAM_AUDIO ветка (loopback) — audio loopback через WebRTC
#   process_message: CMD_ADJUST_BITRATE — WebRTC TWCC управляет битрейтом
#   process_message: CMD_LQ_NEEDED     — simulcast управляется SFU
#   process_message: CMD_NACK_RELAY    — NACK через WebRTC RTCP
#   process_message: request_keyframe  — WebRTC PLI вместо ручного IDR
#
# ─── Что СОХРАНЕНО как заглушки (инкрементальная миграция) ────────────────────
#
#   send_bitrate_feedback()     — stub (no-op), вызывается из ui_video.py
#   send_nack()                 — stub (no-op)
#   request_viewer_keyframe()   — stub (no-op)
#   send_video_frame_chunks()   — stub (no-op), вызывается из video_engine stubs
#   bitrate_adjusted сигнал     — сохранён, никогда не эмитируется
#
# ───────────────────────────────────────────────────────────────────────────────

import asyncio
import base64
import io
import json
import os
import platform
import socket
import struct
import threading
import time
import ctypes
from collections import deque

import numpy as np
import sounddevice as sd
import soundfile as sf
from PyQt6.QtCore import QObject, pyqtSignal, QSettings

from config import (
    resource_path, DEFAULT_PORT_TCP, DEFAULT_PORT_UDP, BUFFER_SIZE,
    UDP_HEADER_STRUCT, UDP_HEADER_SIZE,
    CMD_SOUNDBOARD, FLAG_STREAM_VOICES, FLAG_LOOPBACK_AUDIO,
    STREAM_VOICE_HEADER_STRUCT, STREAM_VOICE_HEADER_SIZE,
    FLAG_WHISPER,
    CMD_NUDGE_VOTE, CMD_PLAY_NUDGE, CMD_NUDGE_TRIGGERED, NUDGE_SOUND_PATH,
    CMD_FILE_OFFER, CMD_FILE_OFFER_ROOM,
    CMD_WEBRTC_OFFER, CMD_WEBRTC_ANSWER, CMD_WEBRTC_ICE,
    WEBRTC_ICE_TIMEOUT,
    CMD_SERVER_MIGRATE,
)

MAX_SILENT_RECONNECT_ATTEMPTS = 4
RECONNECT_DELAY               = 3.0

# Устанавливаем точность системного таймера в 1 мс на Windows.
# Без этого time.sleep(0.001) может спать 10-15 мс — аудио глитчи.
if platform.system() == "Windows":
    try:
        winmm = ctypes.WinDLL('winmm')
        winmm.timeBeginPeriod(1)
    except Exception:
        pass

# ── Опциональный aiortc (для WebRTC PC) ──────────────────────────────────────
try:
    from aiortc import (
        RTCPeerConnection, RTCSessionDescription,
        RTCConfiguration,
    )
    AIORTC_AVAILABLE = True
except ImportError:
    AIORTC_AVAILABLE = False
    RTCPeerConnection = None
    RTCSessionDescription = None
    RTCConfiguration = None
    print("[Net] WARNING: aiortc не установлен — WebRTC функции недоступны")


class NetworkClient(QObject):
    connected           = pyqtSignal(dict)
    global_state_update = pyqtSignal(dict)
    error_occurred      = pyqtSignal(str)

    connection_lost     = pyqtSignal()
    connection_restored = pyqtSignal()
    reconnect_failed    = pyqtSignal()

    # Эмитит (from_nick) при получении soundboard-пакета от сервера
    soundboard_played   = pyqtSignal(str)

    # Сигналы фичи «Пнуть»
    nudge_received  = pyqtSignal()
    nudge_triggered = pyqtSignal(str, str)

    # ABR-сигнал (сохранён для совместимости, не эмитируется после рефакторинга)
    # ui_main.py подключается к нему — подключение станет no-op до Шага 6.
    bitrate_adjusted = pyqtSignal(int)

    # Входящее предложение файловой передачи
    file_offer_received = pyqtSignal(dict)

    # ── Встроенный сервер: сигналы миграции хоста ────────────────────────────
    # become_host      — нам нужно стать новым хостом (запустить embedded server).
    #                    Эмитируется при получении CMD_SERVER_MIGRATE с нашим uid
    #                    или при авто-переключении (мы первые в host_order).
    # server_migrating — получен IP нового хоста; UI показывает индикатор
    #                    переподключения, network_engine сам reconnect'ится.
    become_host      = pyqtSignal()
    server_migrating = pyqtSignal(str)   # new_host_ip

    def __init__(self, audio):
        super().__init__()
        self.audio  = audio
        self.video  = None

        self.server_addr  = None
        self.running      = False
        self.current_ping = 0
        self.packets_sent      = 0
        self.packets_received  = 0

        self._ip     = None
        self._nick   = None
        self._avatar = None

        self._is_connected       = False
        self._reconnecting       = False
        self._reconnect_attempts = 0

        # UID стримера, которого смотрит клиент (0 = не смотрит)
        self._watching_streamer_uid: int = 0

        # Флаг воспроизведения soundboard (anti-spam)
        self._sb_playing = threading.Event()

        # ── Встроенный сервер: состояние хост-очереди и миграции ─────────────
        # _host_order     — UID в порядке входа на сервер (хранится локально).
        #                   Используется при авто-переключении хоста.
        # _server_host_uid — uid текущего хозяина сервера (host_order[0]).
        # _migration_pending — получен CMD_SERVER_MIGRATE, ждём запуска нового хоста.
        self._host_order:       list[int] = []
        self._server_host_uid:  int       = 0
        self._migration_pending: bool     = False   # уже стартовали авто-переход

        # --- WebRTC ---
        # asyncio event loop WebRTC (создаётся один раз при первом подключении)
        self._webrtc_loop: asyncio.AbstractEventLoop | None = None

        # RTCPeerConnection стримера (если текущий клиент стримит)
        self._streamer_pc = None

        # RTCPeerConnection зрителя (если текущий клиент смотрит)
        self._viewer_pc   = None

        self._init_sockets()

    # ------------------------------------------------------------------
    # Сокеты
    # ------------------------------------------------------------------
    def _init_sockets(self):
        # FIX #2: явно закрываем старые сокеты перед созданием новых.
        # При каждой неудачной попытке переподключения старые сокеты закрываются.
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
            # Отключаем алгоритм Нейгла: мелкие команды отправляются немедленно.
            self.tcp_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_socket_bound = False
        except Exception as e:
            print(f"[Net] Socket init error: {e}")

    # ------------------------------------------------------------------
    # Soundboard
    # ------------------------------------------------------------------
    def play_soundboard_file(self, filename, data_b64=None, from_nick=None):
        """
        Воспроизвести soundboard-файл через sounddevice.

        Режим 1 (data_b64=None): файл ищется в assets/panel/ по имени.
        Режим 2 (data_b64 задан): аудио декодируется из base64 из памяти.
        Защита от спама: новый звук не запускается пока предыдущий играет.
        """
        try:
            if self._sb_playing.is_set():
                print(f"[Net] Soundboard: пропущен {filename!r} — звук ещё играет")
                return

            raw = int(QSettings("MyVoiceChat", "GlobalSettings").value("soundboard_volume", 40)) / 100.0
            vol = raw ** 2   # квадратичная кривая громкости

            if data_b64:
                try:
                    audio_bytes  = base64.b64decode(data_b64)
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

            threading.Thread(
                target=_play, daemon=True, name="soundboard-play"
            ).start()
            print(
                f"[Net] Playing soundboard: {filename} "
                f"(vol={vol:.3f}, custom={bool(data_b64)}, by={from_nick!r})"
            )
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
        self._reconnecting       = False
        threading.Thread(target=self._connect_initial, daemon=True).start()

    def _connect_initial(self):
        try:
            self._do_connect()
        except Exception as e:
            print(f"[Net] Initial connection failed: {e}")
            self._reconnecting       = True
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

        # UDP буферы: 512 KB (low-latency режим).
        # 8 MB SO_SNDBUF порождали ложный 3000 мс пинг из-за backpressure:
        # при полном буфере ping-пакеты вставали в хвост позади голоса.
        try:
            self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 512 * 1024)
            self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 512 * 1024)
            print("[Net] UDP buffers: 512 KB (low-latency mode)")
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

        # Запускаем WebRTC asyncio loop (один раз при первом подключении)
        self._start_webrtc_loop()

        print("[Net] Connected to server")

    # ------------------------------------------------------------------
    # WebRTC asyncio loop
    # ------------------------------------------------------------------
    def _start_webrtc_loop(self) -> None:
        """
        Запускает asyncio event loop для WebRTC в daemon-потоке.

        Вызывается из _do_connect() при каждом (пере)подключении.
        Если loop уже запущен и не закрыт — ничего не делает.
        Один loop на всё время жизни процесса: переподключение к серверу
        не требует пересоздания loop (WebRTC PC создаются/закрываются внутри).
        """
        if self._webrtc_loop is not None and not self._webrtc_loop.is_closed():
            return   # уже работает

        self._webrtc_loop = asyncio.new_event_loop()

        # Сообщаем VideoEngine loop чтобы DXCamTrack и VideoReceiver
        # могли использовать правильный asyncio loop
        if self.video is not None:
            self.video.set_webrtc_loop(self._webrtc_loop)

        t = threading.Thread(
            target=self._webrtc_loop.run_forever,
            daemon=True,
            name="webrtc-asyncio",
        )
        t.start()
        print("[Net] WebRTC asyncio loop запущен")

    def _run_in_webrtc_loop(self, coro):
        """
        Отправляет корутину в WebRTC asyncio loop из threading-контекста.
        Безопасно вызывать из любого потока.
        Возвращает concurrent.futures.Future или None если loop не готов.
        """
        if self._webrtc_loop is None or self._webrtc_loop.is_closed():
            print("[Net] WebRTC loop не готов — команда проигнорирована")
            return None
        return asyncio.run_coroutine_threadsafe(coro, self._webrtc_loop)

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
            print(
                f"[Net] Reconnect attempt "
                f"{self._reconnect_attempts}/{MAX_SILENT_RECONNECT_ATTEMPTS}..."
            )
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

        print("[Net] All reconnect attempts failed.")
        self._reconnecting = False
        self.reconnect_failed.emit()

        # Авто-переключение хоста: проверяем не нужно ли нам стать хостом
        if self._host_order:
            threading.Thread(
                target=self._auto_host_check, daemon=True, name="auto-host"
            ).start()

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
    # Миграция сервера (встроенный режим)
    # ------------------------------------------------------------------
    def _migrate_reconnect(self) -> None:
        """
        Выполняет переподключение к новому хосту после CMD_SERVER_MIGRATE.

        Ждёт 1.5 сек (новый хост успевает поднять сервер), затем запускает
        обычный reconnect_loop к новому _ip.

        Не запускается если мы сами становимся хостом (become_host.emit).
        """
        time.sleep(1.5)
        if not self._migration_pending:
            return   # уже переподключились другим способом
        self._migration_pending  = False
        self._reconnecting       = True
        self._reconnect_attempts = 0
        self.running             = False
        self._reconnect_loop()

    def _auto_host_check(self) -> None:
        """
        Запускается после полного провала reconnect_loop (reconnect_failed).

        Логика авто-переключения хоста:
          1. Определяем нашу позицию в _host_order.
          2. Ждём position × 4 секунды (нулевая позиция = сразу).
          3. Проверяем discovery: может кто-то уже поднял сервер?
          4. Если нашли → reconnect к нему.
          5. Если не нашли И мы первые в очереди → emit become_host.
          6. Иначе → ждём ещё, потом снова discovery (max 60 сек).

        Такой механизм гарантирует что сервер поднимется ровно у одного
        участника даже без какой-либо центральной координации.
        """
        try:
            my_uid = getattr(self.audio, 'my_uid', 0)
            try:
                my_pos = self._host_order.index(my_uid)
            except ValueError:
                my_pos = -1   # нас нет в очереди (незнакомый пользователь)

            if my_pos < 0:
                # Не в очереди запасных хостов → периодически ищем новый сервер
                self._discovery_reconnect_loop()
                return

            # Ждём своей очереди: позиция × 4 сек
            wait_sec = my_pos * 4.0
            if wait_sec > 0:
                print(
                    f"[Net] Auto-host: позиция {my_pos}, "
                    f"ждём {wait_sec:.0f}с..."
                )
                time.sleep(wait_sec)

            # Проверяем: не появился ли сервер пока ждали
            from server_discovery import ServerDiscovery
            discovered = ServerDiscovery().discover(timeout=2.0)
            if discovered:
                print(
                    f"[Net] Auto-host: обнаружен сервер "
                    f"{discovered['ip']} (хост {discovered['host_nick']!r})"
                )
                self._ip = discovered['ip']
                self._migration_pending  = False
                self._reconnecting       = True
                self._reconnect_attempts = 0
                self._reconnect_loop()
                return

            # Сервер не найден — становимся хостом
            print(f"[Net] Auto-host: становимся хостом (позиция {my_pos})")
            self.become_host.emit()

        except Exception as e:
            print(f"[Net] _auto_host_check error: {e}")

    def _discovery_reconnect_loop(self) -> None:
        """
        Периодически ищет новый сервер через UDP discovery (для не-первых в очереди).
        Выполняется в daemon-потоке, не блокирует UI.
        """
        from server_discovery import ServerDiscovery
        max_elapsed = 90.0   # максимум 90 секунд ожидания
        elapsed     = 0.0
        probe_interval = 3.5

        print("[Net] Discovery reconnect loop запущен...")
        while elapsed < max_elapsed:
            discovered = ServerDiscovery().discover(timeout=probe_interval)
            if discovered:
                print(f"[Net] Discovery: найден сервер {discovered['ip']}")
                self._ip = discovered['ip']
                self._migration_pending  = False
                self._reconnecting       = True
                self._reconnect_attempts = 0
                self._reconnect_loop()
                return
            elapsed += probe_interval

        # Так и не нашли за 90 секунд — испускаем сигнал ещё раз (UI покажет кнопку)
        print("[Net] Discovery reconnect: сервер не найден за 90 сек")
        self.reconnect_failed.emit()

    def send_server_transfer(self, target_uid: int) -> None:
        """
        Инициирует передачу хостинга другому участнику.
        Отправляет CMD_SERVER_TRANSFER серверу с uid цели.
        Должен вызываться только хостом (server_host_uid == audio.my_uid).
        """
        self.send_json({'action': 'server_transfer', 'target_uid': target_uid})
        print(f"[Net] server_transfer → target_uid={target_uid}")

    # ------------------------------------------------------------------
    # Стриминг — WebRTC
    # ------------------------------------------------------------------
    def start_streaming_webrtc(self, settings: dict | None = None) -> None:
        """
        Запускает WebRTC-стрим.

        Порядок операций:
        1. Создаёт DXCamTrack через VideoEngine.start_streaming(settings).
        2. Создаёт RTCPeerConnection и добавляет трек.
        3. Создаёт WebRTC offer.
        4. Отправляет offer серверу через TCP (CMD_WEBRTC_OFFER, role="streamer").
        5. Сервер создаёт PC на своей стороне, отвечает CMD_WEBRTC_ANSWER.
        6. process_message принимает answer → _handle_streamer_answer_coro.

        Должен вызываться после CMD_STREAM_START (чтобы сервер знал о стриме).
        """
        if not AIORTC_AVAILABLE:
            print("[Net] start_streaming_webrtc: aiortc не установлен")
            return
        if self.video is None:
            print("[Net] start_streaming_webrtc: VideoEngine не установлен")
            return
        if self._webrtc_loop is None:
            print("[Net] start_streaming_webrtc: WebRTC loop не запущен")
            return

        # Запускаем захват экрана через VideoEngine (создаёт DXCamTrack)
        if not self.video.start_streaming(settings):
            print("[Net] start_streaming_webrtc: VideoEngine.start_streaming() вернул False")
            return

        self._run_in_webrtc_loop(self._start_streaming_coro())

    async def _start_streaming_coro(self) -> None:
        """
        Корутина создания WebRTC PC стримера.

        Закрывает старый PC если был (переподключение без перезапуска).
        Добавляет DXCamTrack из VideoEngine.
        Создаёт offer → ждёт ICE gathering → отправляет серверу.
        """
        if self._streamer_pc is not None:
            try:
                await self._streamer_pc.close()
            except Exception:
                pass

        cfg = RTCConfiguration(iceServers=[])
        pc  = RTCPeerConnection(cfg)
        self._streamer_pc = pc

        # Добавляем видеотрек (DXCamTrack)
        dxcam_track = self.video.get_dxcam_track() if self.video else None
        if dxcam_track is None:
            print("[Net] _start_streaming_coro: DXCamTrack недоступен")
            return

        pc.addTrack(dxcam_track)

        @pc.on("icecandidate")
        def on_ice(candidate):
            if candidate:
                self.send_json({
                    'action': CMD_WEBRTC_ICE,
                    'role':   'streamer',
                    'candidate': {
                        'sdpMid':        candidate.sdpMid,
                        'sdpMLineIndex': candidate.sdpMLineIndex,
                        'candidate':     candidate.candidate,
                    },
                })

        @pc.on("connectionstatechange")
        async def on_state():
            state = pc.connectionState
            print(f"[Net] Стример PC state → {state}")
            if state in ("failed", "disconnected"):
                print("[Net] Стример PC потерян — WebRTC отключился")

        try:
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)

            # Ждём ICE gathering (host-only = быстро, ~50 мс)
            await self._wait_ice_gathering(pc)

            self.send_json({
                'action': CMD_WEBRTC_OFFER,
                'role':   'streamer',
                'sdp':    pc.localDescription.sdp,
                'type':   pc.localDescription.type,
            })
            print("[Net] WebRTC offer отправлен серверу (стример)")

        except Exception as e:
            print(f"[Net] _start_streaming_coro error: {e}")
            self._streamer_pc = None

    async def _handle_streamer_answer_coro(self, sdp: str, sdp_type: str) -> None:
        """
        Принимает WebRTC answer от сервера (ответ на наш offer стримера).
        Завершает ICE negotiation на стороне стримера.
        """
        if self._streamer_pc is None:
            print("[Net] _handle_streamer_answer: нет активного streamer PC")
            return
        try:
            await self._streamer_pc.setRemoteDescription(
                RTCSessionDescription(sdp=sdp, type=sdp_type)
            )
            print("[Net] Streamer PC: WebRTC answer принят, ICE завершается")
        except Exception as e:
            print(f"[Net] _handle_streamer_answer error: {e}")

    def stop_streaming_webrtc(self) -> None:
        """
        Останавливает WebRTC-стрим.
        Закрывает PC стримера и останавливает DXCamTrack.
        Должен вызываться вместе с (или после) CMD_STREAM_STOP.
        """
        if self._streamer_pc is not None:
            self._run_in_webrtc_loop(self._close_pc_coro(self._streamer_pc))
            self._streamer_pc = None

        if self.video:
            self.video.stop_streaming()

    # ------------------------------------------------------------------
    # Просмотр стрима — WebRTC
    # ------------------------------------------------------------------
    def start_watching(self, streamer_uid: int):
        """
        Регистрируем начало просмотра стрима.

        Отправляет TCP stream_watch_start.
        Сервер создаёт viewer PC через WebRTCSFU и присылает WebRTC offer.
        Offer обрабатывается в process_message → _handle_viewer_offer_coro.
        """
        self._watching_streamer_uid = streamer_uid
        self.send_json({
            'action':      'stream_watch_start',
            'streamer_uid': streamer_uid,
        })
        print(f"[Net] start_watching → streamer_uid={streamer_uid}")

    def stop_watching(self):
        """
        Регистрируем конец просмотра стрима.
        Закрывает viewer PC и отправляет stream_watch_stop серверу.
        """
        streamer_uid = self._watching_streamer_uid
        self._watching_streamer_uid = 0

        # Закрываем viewer PC
        if self._viewer_pc is not None:
            self._run_in_webrtc_loop(self._close_pc_coro(self._viewer_pc))
            self._viewer_pc = None

        if streamer_uid:
            self.send_json({
                'action':      'stream_watch_stop',
                'streamer_uid': streamer_uid,
            })

        # Останавливаем VideoReceiver для этого стримера
        if self.video and streamer_uid:
            self.video.stop_viewer_for_uid(streamer_uid)

    async def _handle_viewer_offer_coro(
        self, streamer_uid: int, sdp: str, sdp_type: str
    ) -> None:
        """
        Принимает WebRTC offer от сервера (SFU создал PC со треками стримера).

        1. Закрывает старый viewer PC (если был).
        2. Создаёт новый RTCPeerConnection.
        3. on("track") → VideoEngine.add_receiver(streamer_uid, track).
        4. setRemoteDescription(offer) → createAnswer → setLocalDescription.
        5. Ждёт ICE gathering → отправляет answer серверу.
        """
        # Закрываем старый PC зрителя
        if self._viewer_pc is not None:
            try:
                await self._viewer_pc.close()
            except Exception:
                pass

        cfg = RTCConfiguration(iceServers=[])
        pc  = RTCPeerConnection(cfg)
        self._viewer_pc = pc

        @pc.on("track")
        def on_track(track):
            print(
                f"[Net] Viewer PC: получен трек kind={track.kind} "
                f"от стримера uid={streamer_uid}"
            )
            if track.kind == "video" and self.video:
                self.video.add_receiver(streamer_uid, track)

        @pc.on("icecandidate")
        def on_ice(candidate):
            if candidate:
                self.send_json({
                    'action': CMD_WEBRTC_ICE,
                    'role':   'viewer',
                    'candidate': {
                        'sdpMid':        candidate.sdpMid,
                        'sdpMLineIndex': candidate.sdpMLineIndex,
                        'candidate':     candidate.candidate,
                    },
                })

        @pc.on("connectionstatechange")
        async def on_state():
            state = pc.connectionState
            print(f"[Net] Viewer PC state → {state}")
            if state in ("failed", "disconnected"):
                print(f"[Net] Viewer PC для uid={streamer_uid} потерян")

        try:
            await pc.setRemoteDescription(
                RTCSessionDescription(sdp=sdp, type=sdp_type)
            )
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)

            await self._wait_ice_gathering(pc)

            self.send_json({
                'action': CMD_WEBRTC_ANSWER,
                'sdp':    pc.localDescription.sdp,
                'type':   pc.localDescription.type,
            })
            print(f"[Net] WebRTC answer отправлен серверу (зритель uid={streamer_uid})")

        except Exception as e:
            print(f"[Net] _handle_viewer_offer_coro error: {e}")
            if self._viewer_pc is pc:
                self._viewer_pc = None

    async def _handle_ice_candidate_coro(
        self, role: str, candidate_dict: dict
    ) -> None:
        """
        Добавляет входящий ICE-кандидат к нужному PC.
        role="streamer" → _streamer_pc
        role="viewer"   → _viewer_pc
        """
        pc = self._streamer_pc if role == "streamer" else self._viewer_pc
        if pc is None:
            return
        try:
            from aiortc import RTCIceCandidate
            cand = RTCIceCandidate(
                sdpMid=candidate_dict.get('sdpMid'),
                sdpMLineIndex=candidate_dict.get('sdpMLineIndex'),
                candidate=candidate_dict.get('candidate'),
            )
            await pc.addIceCandidate(cand)
        except Exception as e:
            print(f"[Net] addIceCandidate ({role}): {e}")

    @staticmethod
    async def _close_pc_coro(pc) -> None:
        """Закрывает RTCPeerConnection в asyncio-контексте."""
        try:
            await pc.close()
        except Exception:
            pass

    @staticmethod
    async def _wait_ice_gathering(
        pc, timeout: float = WEBRTC_ICE_TIMEOUT
    ) -> None:
        """
        Ждёт завершения ICE gathering с таймаутом.
        На RadminVPN (host-only ICE) завершается за ~50–200 мс.
        """
        loop    = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while pc.iceGatheringState != "complete":
            if loop.time() >= deadline:
                print(f"[Net] ICE gathering timeout ({timeout}s) — продолжаем")
                break
            await asyncio.sleep(0.05)

    # ------------------------------------------------------------------
    # Приём UDP-пакетов
    # ------------------------------------------------------------------
    def udp_receive_loop(self):
        """
        Принимает UDP-пакеты.

        После рефакторинга обрабатывает:
          — Ping/Pong (flags=254): EWMA RTT.
          — FLAG_STREAM_VOICES: голоса участников для зрителей (Mix Minus UDP).
          — FLAG_WHISPER: шёпот (приватный голос).
          — Голос комнаты: обычный аудио.

        Видео (FLAG_VIDEO) и loopback-аудио (FLAG_STREAM_AUDIO) удалены —
        они передаются через WebRTC треки.
        """
        while self.running:
            try:
                data, addr = self.udp_sock.recvfrom(BUFFER_SIZE)
                if len(data) < UDP_HEADER_SIZE:
                    continue

                uid, ts, seq, flags = UDP_HEADER_STRUCT.unpack(data[:UDP_HEADER_SIZE])

                if flags == 254:
                    # Pong — измеряем RTT (EWMA)
                    self.packets_received += 1
                    delay = (time.time() - ts) * 1000
                    if self.current_ping == 0:
                        self.current_ping = int(delay)
                    else:
                        self.current_ping = int(self.current_ping * 0.7 + delay * 0.3)

                elif flags & FLAG_STREAM_VOICES:
                    # Голосовой поток стрима (Mix Minus без DSP).
                    # Payload: [speaker_uid: 4 байта] + [opus].
                    # Свой голос отбрасываем.
                    # UDP-путь для FLAG_STREAM_VOICES сохранён (план рекомендация В).
                    if len(data) < UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE:
                        continue
                    speaker_uid, = STREAM_VOICE_HEADER_STRUCT.unpack(
                        data[UDP_HEADER_SIZE: UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE]
                    )
                    if speaker_uid == self.audio.my_uid:
                        continue
                    opus_payload = data[UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE:]
                    self.audio.add_incoming_stream_packet(
                        speaker_uid, seq, opus_payload, flags
                    )

                elif flags & FLAG_WHISPER:
                    # Шёпот — приватный голос от sender к нам.
                    # Payload: [target_uid: 4 байта] + [opus].
                    # Отбрасываем 4-байтовый заголовок target_uid перед декодированием.
                    if len(data) < UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE:
                        continue
                    opus_payload = data[UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE:]
                    self.audio.add_incoming_whisper_packet(uid, seq, opus_payload)

                else:
                    # Обычный голос чата
                    self.audio.add_incoming_packet(uid, seq, data[UDP_HEADER_SIZE:], flags)

            except Exception as e:
                if self.running:
                    import traceback
                    print(f"[Net] UDP receive error: {e}\n{traceback.format_exc()}")
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
                    header = UDP_HEADER_STRUCT.pack(
                        self.audio.my_uid, time.time(), 0, flags
                    )
                    self.udp_sock.sendto(header, self.server_addr)
                except Exception as e:
                    print(f"[Net] Keepalive error: {e}")
            time.sleep(1)

    def ping_loop(self):
        while self.running:
            if self.audio.my_uid != 0:
                try:
                    header = UDP_HEADER_STRUCT.pack(
                        self.audio.my_uid, time.time(), 0, 254
                    )
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
        # JSONDecoder создаём ОДИН РАЗ — stateless, экономим аллокации.
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
        act = msg.get('action')

        if act == 'login_success':
            self.connected.emit(msg)
            print(f"[Net] Login success, UID: {msg.get('uid')}")

        elif act == 'sync_users':
            # Обновляем локальный кэш host_order и server_host_uid
            self._host_order      = msg.get('host_order', [])
            self._server_host_uid = msg.get('server_host_uid', 0)
            self.global_state_update.emit(msg.get('all_users', {}))

        elif act == 'play_soundboard':
            self.play_soundboard_file(
                msg.get('file'), msg.get('data_b64'), msg.get('from_nick')
            )

        # ── WebRTC: offer от сервера → мы зритель ─────────────────────────
        elif act == CMD_WEBRTC_OFFER:
            role         = msg.get('role', '')
            sdp          = msg.get('sdp')
            sdp_type     = msg.get('type', 'offer')
            streamer_uid = msg.get('streamer_uid', self._watching_streamer_uid)

            if role == 'viewer' and sdp and AIORTC_AVAILABLE:
                # Сервер прислал нам offer — мы зритель, создаём answer
                self._run_in_webrtc_loop(
                    self._handle_viewer_offer_coro(streamer_uid, sdp, sdp_type)
                )

        # ── WebRTC: answer от сервера → мы стример ────────────────────────
        elif act == CMD_WEBRTC_ANSWER:
            sdp      = msg.get('sdp')
            sdp_type = msg.get('type', 'answer')
            if sdp and AIORTC_AVAILABLE:
                # Сервер ответил на наш offer стримера
                self._run_in_webrtc_loop(
                    self._handle_streamer_answer_coro(sdp, sdp_type)
                )

        # ── WebRTC: ICE кандидат ───────────────────────────────────────────
        elif act == CMD_WEBRTC_ICE:
            candidate = msg.get('candidate')
            role      = msg.get('role', '')
            if candidate and AIORTC_AVAILABLE:
                self._run_in_webrtc_loop(
                    self._handle_ice_candidate_coro(role, candidate)
                )

        # ── Nudge ──────────────────────────────────────────────────────────
        elif act == CMD_PLAY_NUDGE:
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

        # ── Файловая передача ──────────────────────────────────────────────
        elif act in ('file_offer', 'file_offer_room'):
            self.file_offer_received.emit(msg)

        # ── Миграция сервера (встроенный режим) ────────────────────────────
        elif act == CMD_SERVER_MIGRATE:
            new_host_uid = msg.get('new_host_uid', 0)
            new_host_ip  = msg.get('new_host_ip', '')
            print(
                f"[Net] CMD_SERVER_MIGRATE: новый хост uid={new_host_uid}, "
                f"ip={new_host_ip!r}"
            )
            my_uid = getattr(self.audio, 'my_uid', 0)
            if new_host_uid == my_uid:
                # Мы — новый хост: сигнализируем UI запустить embedded server
                self.become_host.emit()
            elif new_host_ip:
                # Переподключаемся к новому хосту
                self._ip = new_host_ip
                self._migration_pending = True
                self.server_migrating.emit(new_host_ip)
                # Небольшая пауза: новый хост успевает запустить сервер
                threading.Thread(
                    target=self._migrate_reconnect,
                    daemon=True,
                    name="net-migrate",
                ).start()

        # ── Устаревшие UDP-видео команды (заглушки для совместимости) ──────
        # request_keyframe, CMD_ADJUST_BITRATE, CMD_LQ_NEEDED, CMD_NACK_RELAY:
        # WebRTC управляет этим автоматически через RTCP PLI / TWCC.
        # Если сервер старой версии прислал — тихо игнорируем.

    # ------------------------------------------------------------------
    # Утилиты
    # ------------------------------------------------------------------
    def send_json(self, data):
        try:
            self.tcp_sock.sendall(json.dumps(data).encode('utf-8'))
        except Exception as e:
            print(f"[Net] Send JSON error: {e}")

    def update_user_info(self, nick, avatar):
        self.send_json({"action": "update_user", "nick": nick, "avatar": avatar})

    def send_status_update(self, mute, deaf):
        self.send_json({"action": "update_status", "mute": mute, "deaf": deaf})

    def send_presence_update(self, status_icon: str, status_text: str):
        """
        Отправляет серверу новый «статус дела» пользователя.
        status_icon: имя SVG-файла из assets/status/ или '' (нет статуса).
        status_text: произвольная подпись ≤ 30 символов или ''.
        """
        self.send_json({
            "action":      "update_presence",
            "status_icon": status_icon,
            "status_text": status_text,
        })

    def set_video_engine(self, video) -> None:
        """
        Регистрирует VideoEngine.

        Если WebRTC loop уже запущен — передаём ему loop сразу.
        Если нет — loop будет установлен в _start_webrtc_loop() при подключении.
        """
        self.video = video
        if self._webrtc_loop is not None and not self._webrtc_loop.is_closed():
            video.set_webrtc_loop(self._webrtc_loop)
        print("[Net] VideoEngine registered")

    # ------------------------------------------------------------------
    # Файловая передача P2P — только сигнализация через сервер
    # ------------------------------------------------------------------
    def send_file_offer(
        self, target_uid: int, filename: str,
        filesize: int, sender_port: int, token: str
    ) -> None:
        """Личная передача файла: отправляем offer одному получателю."""
        self.send_json({
            "action":      "file_offer",
            "target_uid":  target_uid,
            "filename":    filename,
            "filesize":    filesize,
            "sender_port": sender_port,
            "token":       token,
        })

    def send_file_offer_room(
        self, filename: str, filesize: int,
        sender_port: int, token: str
    ) -> None:
        """Массовая передача: offer всем в комнате кроме отправителя."""
        self.send_json({
            "action":      "file_offer_room",
            "filename":    filename,
            "filesize":    filesize,
            "sender_port": sender_port,
            "token":       token,
        })

    # ------------------------------------------------------------------
    # Nudge: воспроизведение звука и голосование
    # ------------------------------------------------------------------
    def send_nudge_vote(self, target_uid: int):
        """Отправить голос «Пнуть» для указанного пользователя."""
        self.send_json({
            'action':     CMD_NUDGE_VOTE,
            'target_uid': target_uid,
        })
        print(f"[Net] Nudge vote sent → target_uid={target_uid}")

    def _nudge_get_endpoint_vol(self):
        """
        Возвращает IAudioEndpointVolume дефолтного устройства воспроизведения.

        Поддерживает ОБЕ версии pycaw:
          pycaw < 0.6  — GetSpeakers() возвращает IMMDevice с .Activate()
          pycaw >= 0.6 — GetSpeakers() возвращает AudioDevice-обёртку, IMMDevice в ._dev
        Fallback: comtypes напрямую без pycaw.
        Возвращает IAudioEndpointVolume* или None при любой ошибке.
        """
        # ── Попытка 1: pycaw ──────────────────────────────────────────────
        try:
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            from comtypes import CLSCTX_ALL
            from ctypes import cast, POINTER

            device = AudioUtilities.GetSpeakers()
            if hasattr(device, 'Activate'):
                raw_dev = device
            elif hasattr(device, '_dev'):
                raw_dev = device._dev
            else:
                raise RuntimeError(f"Неизвестный тип GetSpeakers(): {type(device).__name__}")

            iface = raw_dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            return cast(iface, POINTER(IAudioEndpointVolume))

        except ImportError:
            print("[Nudge] pycaw не установлен — пробуем comtypes напрямую")
        except Exception as e:
            print(f"[Nudge] pycaw get_endpoint_vol error: {e}")

        # ── Попытка 2: comtypes напрямую ──────────────────────────────────
        try:
            import comtypes
            import comtypes.client
            from ctypes import cast, POINTER, c_float, c_int, c_uint, HRESULT

            CLSID_MMDeviceEnumerator = comtypes.GUID('{BCDE0395-E52F-467C-8E3D-C4579291692E}')
            IID_IMMDeviceEnumerator  = comtypes.GUID('{A95664D2-9614-4F35-A746-DE8DB63617E6}')
            IID_IMMDevice            = comtypes.GUID('{D666063F-1587-4E43-81F1-B948E807363F}')
            IID_IAudioEndpointVolume = comtypes.GUID('{5CDF2C82-841E-4546-9722-0CF74078229A}')

            class IMMDevice(comtypes.IUnknown):
                _iid_ = IID_IMMDevice
                _methods_ = [
                    comtypes.COMMETHOD([], HRESULT, 'Activate',
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
                _iid_ = IID_IMMDeviceEnumerator
                _methods_ = [
                    comtypes.COMMETHOD([], HRESULT, 'EnumAudioEndpoints',
                        (['in'],  c_uint, 'dataFlow'),
                        (['in'],  c_uint, 'dwStateMask'),
                        (['out'], POINTER(comtypes.IUnknown), 'ppDevices'),
                    ),
                    comtypes.COMMETHOD([], HRESULT, 'GetDefaultAudioEndpoint',
                        (['in'],  c_uint,             'dataFlow'),
                        (['in'],  c_uint,             'role'),
                        (['out'], POINTER(IMMDevice), 'ppEndpoint'),
                    ),
                ]

            class IAudioEndpointVolumeDirect(comtypes.IUnknown):
                _iid_ = IID_IAudioEndpointVolume
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
                        (['in'], c_uint, 'nChannel'),
                        (['in'], c_float, 'fLevelDB'),
                        (['in'], comtypes.c_void_p, 'pguidEventContext')),
                    comtypes.COMMETHOD([], HRESULT, 'SetChannelVolumeLevelScalar',
                        (['in'], c_uint, 'nChannel'),
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
                CLSID_MMDeviceEnumerator, interface=IMMDeviceEnumerator,
            )
            device = enumerator.GetDefaultAudioEndpoint(0, 0)
            iface  = device.Activate(IID_IAudioEndpointVolume, 0x17, None)
            return cast(iface, POINTER(IAudioEndpointVolumeDirect))

        except Exception as e:
            print(f"[Nudge] comtypes direct error: {e}")

        return None

    def _nudge_boost_volume(self) -> tuple:
        """
        Снимает системный мьют и поднимает мастер-громкость Windows.
        Возвращает (prev_scalar, was_muted) для восстановления.
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
        """Восстанавливает мастер-громкость Windows в finally-блоке."""
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
        """
        Воспроизвести Danger.mp3 + системный писк — НЕЗАВИСИМО от deaf/mute.
        Форсирует системную громкость Windows, восстанавливает в finally.
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

    # ------------------------------------------------------------------
    # Устаревшие методы — заглушки для инкрементальной миграции
    # ------------------------------------------------------------------
    # Вызываются из: ui_video.py (_send_abr_feedback → send_bitrate_feedback),
    # видео-движок (заглушки video_engine.py), ui_main.py.
    # После обновления ui_video.py (Шаг 7) и ui_main.py (Шаг 6) — удалить.
    # ------------------------------------------------------------------

    def send_video_frame_chunks(self, chunks: list, flags: int = 0) -> None:
        """УСТАРЕЛО: видео передаётся через WebRTC DXCamTrack. Заглушка."""
        pass

    def send_video_packet(self, payload) -> None:
        """УСТАРЕЛО: compat wrapper. Заглушка."""
        pass

    def send_bitrate_feedback(self, ping_ms: int) -> None:
        """
        УСТАРЕЛО: WebRTC управляет битрейтом через TWCC автоматически.
        Вызывается из VideoWindow._send_abr_feedback() — no-op до Шага 7.
        """
        pass

    def send_nack(self, streamer_uid: int, frame_id: int, chunk_idx: int) -> None:
        """УСТАРЕЛО: NACK через WebRTC RTCP. Заглушка."""
        pass

    def request_viewer_keyframe(self, streamer_uid: int) -> None:
        """УСТАРЕЛО: WebRTC PLI (Picture Loss Indication). Заглушка."""
        pass