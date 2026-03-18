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
    CMD_QUICK_MSG, QUICK_MSG_MAX_LEN,
    CMD_HOST_MUTE, CMD_FORCE_MUTED,
    CMD_CHAT_MSG, CMD_CHAT_HISTORY, CMD_CHAT_HISTORY_REQ,
    CHAT_MSG_MAX_LEN, CHAT_HISTORY_MAX,
    CMD_CHAT_MEDIA, CHAT_MEDIA_MAX_B64,
)

MAX_SILENT_RECONNECT_ATTEMPTS = 2    # было 4: 4×3с=12с → 2×1с=2с до auto_host_check
RECONNECT_DELAY               = 1.0  # было 3.0с

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

    # Быстрый чат: (uid, from_nick, text)
    quick_msg_received  = pyqtSignal(int, str, str)

    # Постоянный чат: одно сообщение (dict) или список (history)
    chat_msg_received     = pyqtSignal(dict)   # новое сообщение
    chat_history_received = pyqtSignal(list)   # история при подключении
    chat_media_received   = pyqtSignal(dict)   # медиа-вложение (фото/файл)

    # ── Встроенный сервер: сигналы миграции хоста ────────────────────────────
    # become_host      — нам нужно стать новым хостом (запустить embedded server).
    #                    Эмитируется при получении CMD_SERVER_MIGRATE с нашим uid
    #                    или при авто-переключении (мы первые в host_order).
    # server_migrating — получен IP нового хоста; UI показывает индикатор
    #                    переподключения, network_engine сам reconnect'ится.
    become_host      = pyqtSignal()
    server_migrating = pyqtSignal(str)   # new_host_ip

    # ── Каналы и мульти-серверная архитектура ────────────────────────────────
    channel_created      = pyqtSignal(str)        # channel_name
    channel_deleted      = pyqtSignal(str)        # channel_name
    join_room_denied     = pyqtSignal(str, str)   # room, reason
    channel_auth_ok      = pyqtSignal(str)        # channel_name
    channel_list_updated = pyqtSignal(list)       # list[dict]

    # Хост выключил наш микрофон (CMD_FORCE_MUTED).
    # MainWindow применяет mute. Кнопки НЕ блокируются — участник может включить сам.
    force_muted = pyqtSignal()

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

        # История чата: список dict {uid, nick, avatar, text, ts, room}.
        # Хранится в памяти, не персистентна. Максимум CHAT_HISTORY_MAX записей.
        # Обновляется при каждом входящем CMD_CHAT_MSG и CMD_CHAT_HISTORY.
        self._chat_history: list[dict] = []

        # ── Встроенный сервер: состояние хост-очереди и миграции ─────────────        # _host_order      — UID участников в порядке входа на сервер.
        # _server_host_uid — UID текущего хоста (host_order[0]).
        # _migration_pending — получен CMD_SERVER_MIGRATE, ждём нового хоста.
        # _host_order_ips  — UID → RadminVPN IP всех участников группы.
        #                    Обновляется при каждом sync_users.
        #                    Ключевой факт: в нём ТОЛЬКО наши люди — те, кто
        #                    был на нашем сервере. При дропе хоста мы ищем
        #                    нового хоста ТОЛЬКО среди них, не через discovery.
        self._host_order:       list[int]      = []
        self._server_host_uid:  int            = 0
        self._migration_pending: bool          = False
        self._host_order_ips:   dict[int, str] = {}  # uid → ip

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

            # FIX #9: читаем громкость из уже существующего QSettings AudioHandler,
            # вместо создания нового QSettings (обращение к реестру Windows) при
            # каждом воспроизведении. Fallback: создаём один раз если audio недоступен.
            _gs = getattr(self.audio, 'global_settings', None)
            if _gs is None:
                _gs = QSettings("MyVoiceChat", "GlobalSettings")
            raw = int(_gs.value("soundboard_volume", 40)) / 100.0
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
                    # FIX: sd.play() открывает новый PortAudio/WASAPI поток → фриз.
                    # Используем внутренний микшер AudioHandler — звук подаётся прямо
                    # в уже открытый 20ms callback без аллокации новых устройств.
                    if hasattr(self.audio, 'play_internal_sound') and self.audio.stream:
                        self.audio.play_internal_sound(data, sr, vol)
                        # Ждём пока звук отыграет (как sd.wait()) чтобы корректно
                        # снять флаг _sb_playing и не наслоить следующий трек.
                        duration = len(data) / sr
                        time.sleep(duration)
                    else:
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

    def fast_switch_to(self, new_ip: str) -> None:
        """
        Быстрое намеренное переключение на другой сервер.

        Отличие от _reconnect_loop:
          — БЕЗ паузы RECONNECT_DELAY (3 сек) между попытками: сеть жива,
            новый сервер уже работает, ждать нечего.
          — MAX_ATTEMPTS = 8 с паузой 0.3 сек: суммарно ~2.5 сек на случай
            если сервер только стартует.
          — Не трогает connection_lost / connection_restored сигналы:
            UI переключается через _on_switch_server который сам показывает
            нужный экран.

        Вызывается из MainWindow._on_switch_server().
        """
        if self._reconnecting:
            print("[Net] fast_switch_to: уже идёт переподключение, пропускаем")
            return
        print(f"[Net] fast_switch_to: → {new_ip}")
        self._ip             = new_ip
        self.running         = False
        self._reconnecting   = True
        self._reconnect_attempts = 0
        threading.Thread(
            target=self._fast_switch_loop,
            daemon=True,
            name="fast-switch",
        ).start()

    def _fast_switch_loop(self) -> None:
        """
        Цикл переподключения для намеренной смены сервера.
        Попытки без паузы RECONNECT_DELAY — сервер уже запущен и доступен.
        """
        MAX_ATTEMPTS = 8
        FAST_DELAY   = 0.35   # сек между попытками
        for attempt in range(1, MAX_ATTEMPTS + 1):
            print(f"[Net] fast_switch attempt {attempt}/{MAX_ATTEMPTS} → {self._ip}")
            try:
                self._init_sockets()
                self._do_connect()
                self._reconnecting       = False
                self._reconnect_attempts = 0
                print("[Net] fast_switch: подключено успешно!")
                self.connection_restored.emit()
                return
            except Exception as e:
                print(f"[Net] fast_switch attempt {attempt} failed: {e}")
            time.sleep(FAST_DELAY)

        # Все попытки провалились — показываем ошибку
        self._reconnecting = False
        print("[Net] fast_switch: все попытки исчерпаны")
        self.reconnect_failed.emit()

    # ------------------------------------------------------------------
    # Миграция сервера (встроенный режим)
    # ------------------------------------------------------------------
    def _migrate_reconnect(self) -> None:
        """
        Переподключение к новому хосту после CMD_SERVER_MIGRATE.

        Порядок операций важен:
          1. running=False ПЕРВЫМ — если tcp_listen выйдет раньше (FIN от сервера),
             он увидит running=False и не запустит _on_connection_lost.
          2. _migration_pending=False — сигнал что мы обрабатываем миграцию.
          3. _reconnecting=False — сбрасываем флаг для fast_switch_to.
          4. fast_switch_to — 8 попыток × 0.35с ≈ 2.8с окно.
             Новый хост стартует за ~150–300мс (QTimer 150мс + bind).
             fast_switch_to подключится с 1-2 попытки.

        Убрана статичная задержка 2.5с (Bug A).
        """
        if not self._migration_pending:
            return
        # Шаг 1: running=False ДО сброса _migration_pending
        self.running = False
        # Шаг 2-3: сбрасываем флаги
        self._migration_pending = False
        self._reconnecting      = False
        # Шаг 4: быстрое переподключение к новому хосту
        self.fast_switch_to(self._ip)

    def _auto_host_check(self) -> None:
        """
        Запускается после полного провала reconnect_loop (хост упал / обрыв сети).

        ИСПРАВЛЕНИЕ: раньше использовался ServerDiscovery().discover() который
        находил ЛЮБОЙ сервер в RadminVPN — включая чужих (Владик и брат).
        Участники группы улетали к посторонним людям.

        Новая логика работает только внутри своей группы:
          1. Берём _host_order (порядок входа) и _host_order_ips (их IP).
             Оба поля обновлялись при каждом sync_users с нашего сервера.
          2. Убираем упавшего хоста из рассмотрения.
          3. Если остались другие участники — они по очереди пробуют стать хостом:
             - Первый в оставшейся очереди → сразу emit become_host.
             - Остальные ждут (позиция - 1) × 3 сек, затем пробуют подключиться
               к первому. Если он поднял сервер — подключаются. Нет — следующий
               в очереди тоже станет хостом, и тогда подключаются к нему.
          4. Никаких ServerDiscovery — только наши IP.
        """
        try:
            my_uid   = getattr(self.audio, 'my_uid', 0)
            old_host = self._server_host_uid  # упавший хост

            # Оставшиеся участники группы (без упавшего хоста)
            remaining_order = [uid for uid in self._host_order if uid != old_host]

            if not remaining_order:
                print("[Net] Auto-host: группа пуста после дропа хоста, становимся хостом")
                self.become_host.emit()
                return

            try:
                my_pos = remaining_order.index(my_uid)
            except ValueError:
                # Нас нет в очереди — просто ждём пока первый поднимет сервер
                first_ip = self._host_order_ips.get(remaining_order[0], '')
                print(f"[Net] Auto-host: нас нет в очереди, ждём первого ({first_ip})")
                if first_ip:
                    self._wait_and_connect_group(first_ip)
                return

            if my_pos == 0:
                # Мы первые в оставшейся очереди → немедленно становимся хостом
                print(f"[Net] Auto-host: мы первые в группе из {len(remaining_order)}, "
                      f"запускаем сервер")
                self.become_host.emit()
                return

            # Ждём (позиция) × 1.5 сек — первый должен успеть поднять сервер.
            # Было × 3.0с: при 2 клиентах это давало 3с лишнего ожидания.
            # 1.5с: за 2с reconnect_loop + 1.5с ожидание = 3.5с суммарно для клиента 3.
            wait_sec = my_pos * 1.5
            print(f"[Net] Auto-host: позиция {my_pos} в группе, ждём {wait_sec:.1f}с...")
            time.sleep(wait_sec)

            # Пробуем подключиться к кандидатам раньше нас по очереди
            for candidate_uid in remaining_order[:my_pos]:
                candidate_ip = self._host_order_ips.get(candidate_uid, '')
                if not candidate_ip:
                    continue
                print(f"[Net] Auto-host: пробуем uid={candidate_uid} ip={candidate_ip}")
                if self._try_group_connect(candidate_ip):
                    return  # успешно подключились к новому хосту из своей группы

            # Никто не ответил — становимся хостом сами
            print(f"[Net] Auto-host: никто из группы не ответил, берём хостинг")
            self.become_host.emit()

        except Exception as e:
            print(f"[Net] _auto_host_check error: {e}")

    def _try_group_connect(self, ip: str, max_attempts: int = 6) -> bool:
        """
        Пробует подключиться к участнику своей группы (новый хост).
        max_attempts × 0.5 сек = 3 сек окно ожидания.
        Возвращает True если порт доступен и запущен fast_switch_to.

        FIX (Bug D): было _reconnect_loop() после probe → добавляло 3с sleep
        в начале. Теперь fast_switch_to: 8 попыток × 0.35с без начального sleep.
        """
        from config import DEFAULT_PORT_TCP
        import socket as _sock
        for attempt in range(1, max_attempts + 1):
            try:
                s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
                s.settimeout(1.0)
                s.connect((ip, DEFAULT_PORT_TCP))
                s.close()
                print(f"[Net] _try_group_connect: {ip} ответил (попытка {attempt})")
                # fast_switch_to сам выставит running=False и _reconnecting=True
                self._reconnecting = False   # сброс чтобы fast_switch_to прошёл guard
                self.fast_switch_to(ip)
                return True
            except Exception:
                pass
            time.sleep(0.5)
        print(f"[Net] _try_group_connect: {ip} не поднял сервер")
        return False

    def _wait_and_connect_group(self, ip: str) -> None:
        """
        Ждёт пока первый в очереди поднимет сервер (до 15 сек), затем подключается.
        Для участников не в host_order (например зашли позже).
        """
        print(f"[Net] _wait_and_connect_group: ждём {ip}...")
        if self._try_group_connect(ip, max_attempts=30):  # 30 × 0.5с = 15 сек
            return
        # Так и не поднял — показываем ошибку
        self.reconnect_failed.emit()

    def _discovery_reconnect_loop(self) -> None:
        """
        УСТАРЕЛО: больше не вызывается из _auto_host_check.
        Оставлено только для возможного внешнего использования.
        Ищет любой сервер через UDP broadcast (может найти чужой).
        """
        from server_discovery import ServerDiscovery
        max_elapsed = 90.0
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

        print("[Net] Discovery reconnect: сервер не найден за 90 сек")
        self.reconnect_failed.emit()

    def stop(self) -> None:
        """
        Корректная остановка NetworkClient при закрытии приложения.

        Порядок важен:
          1. running=False — все петли udp/tcp читают флаг и выходят.
          2. Закрываем RTCPeerConnection стримера и зрителя (если активны).
          3. Останавливаем WebRTC asyncio loop (loop.stop → run_forever завершается).
          4. Закрываем TCP/UDP сокеты — разблокирует recv/recvfrom в потоках.

        Потоки daemon=True — они завершатся сами после выхода из цикла.
        Явный join не нужен: ОС освободит ресурсы при выходе процесса.
        Но закрытие сокетов гарантирует выход из блокирующих recv() немедленно,
        без ожидания следующего тайм-аута или пакета.
        """
        print("[Net] stop(): завершаем сетевые потоки...")
        self.running = False
        self._is_connected = False

        # Закрываем WebRTC PeerConnections
        if self._webrtc_loop is not None and not self._webrtc_loop.is_closed():
            if self._streamer_pc is not None:
                asyncio.run_coroutine_threadsafe(
                    self._close_pc_coro(self._streamer_pc), self._webrtc_loop
                )
                self._streamer_pc = None
            if self._viewer_pc is not None:
                asyncio.run_coroutine_threadsafe(
                    self._close_pc_coro(self._viewer_pc), self._webrtc_loop
                )
                self._viewer_pc = None

            # Даём 200мс на закрытие PC, потом останавливаем loop
            import time as _t
            _t.sleep(0.2)
            try:
                self._webrtc_loop.call_soon_threadsafe(self._webrtc_loop.stop)
            except Exception:
                pass

        # Закрываем сокеты — разблокирует блокирующие recv() в потоках
        for attr in ('tcp_sock', 'udp_sock'):
            sock = getattr(self, attr, None)
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

        print("[Net] stop(): готово")

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

        pc.addTrack(dxcam_track)   # HQ: первый видеотрек

        # Simulcast: добавляем LQ трек вторым (SFU идентифицирует по порядку)
        lq_track = self.video.get_lq_track() if self.video else None
        if lq_track is not None:
            pc.addTrack(lq_track)
            print("[Net] Simulcast LQ трек добавлен в RTCPeerConnection")

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
    def start_watching(self, streamer_uid: int, quality: str = 'hq'):
        """
        Регистрируем начало просмотра стрима.

        quality='hq' — запросить HQ поток (default, для быстрых ПК/каналов).
        quality='lq' — запросить LQ поток (слабый ПК или медленный RadminVPN).

        Сервер передаёт quality в SFU.handle_viewer_connect() →
        зрителю маршрутизируется нужный simulcast-поток.
        """
        self._watching_streamer_uid = streamer_uid
        self.send_json({
            'action':       'stream_watch_start',
            'streamer_uid':  streamer_uid,
            'quality':       quality,
        })
        print(f"[Net] start_watching → streamer_uid={streamer_uid}, quality={quality}")

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
        loop     = asyncio.get_running_loop()
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
                    # FIX #1: add_incoming_stream_packet удалён вместе с UDP стрим-аудио.
                    # Mix-Minus голоса зрителей идут через обычный add_incoming_packet —
                    # FLAG_STREAM_VOICES передаётся как flags, декодер тот же Opus.
                    self.audio.add_incoming_packet(
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

            except OSError as e:
                # WinError 10038 — операция на закрытом сокете (другой поток закрыл).
                # WinError 10022 — недопустимый аргумент (сокет уже недействителен).
                # WinError 10054 — соединение сброшено удалённой стороной.
                # Во всех случаях это штатное завершение — break без traceback.
                err = getattr(e, 'winerror', None)
                if not self.running or err in (10038, 10022, 10054):
                    break
                import traceback
                print(f"[Net] UDP receive error: {e}\n{traceback.format_exc()}")
                break
            except Exception as e:
                if self.running:
                    import traceback
                    print(f"[Net] UDP receive error: {e}\n{traceback.format_exc()}")
                # Не break — обычные не-сокетные ошибки могут быть разовыми

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

                # Отправляем текущий RTT серверу (тихо — без sync_users broadcast).
                # Сервер накапливает пинги для _pick_best_host при выборе нового хоста.
                if self.current_ping > 0:
                    try:
                        self.send_json({'action': 'report_ping', 'ping_ms': self.current_ping})
                    except Exception:
                        pass

            time.sleep(3)

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
        # FIX (Bug B): не вызываем _on_connection_lost если идёт миграция.
        # При CMD_SERVER_MIGRATE сервер закрывает наш сокет (FIN/RST) →
        # tcp_listen выходит. Без этой проверки запускается ВТОРОЙ reconnect_loop
        # параллельно с _migrate_reconnect — они конкурируют за сокеты.
        # _migration_pending=True означает что _migrate_reconnect уже занимается
        # переподключением. running=False означает что мы сами инициировали остановку.
        if self.running and not self._migration_pending:
            self._on_connection_lost()

    def process_message(self, msg: dict):
        act = msg.get('action')

        if act == 'login_success':
            self.connected.emit(msg)
            print(f"[Net] Login success, UID: {msg.get('uid')}")
            # Запрашиваем историю чата у хоста (тот кто первый в host_order).
            # Если мы единственный клиент — сервер промолчит, история останется пустой.
            self.send_json({'action': CMD_CHAT_HISTORY_REQ})

        elif act == 'sync_users':
            self._host_order      = msg.get('host_order', [])
            self._server_host_uid = msg.get('server_host_uid', 0)

            # Строим карту uid → ip из all_users.
            # Это IP-адреса наших людей — нужны _auto_host_check при дропе хоста,
            # чтобы пробовать только своих, а не любой сервер через broadcast.
            all_users = msg.get('all_users', {})
            new_ips: dict[int, str] = {}
            for users_in_room in all_users.values():
                for u in users_in_room:
                    u_uid = u.get('uid', 0)
                    u_ip  = u.get('ip',  '')
                    if u_uid and u_ip:
                        new_ips[u_uid] = u_ip
            self._host_order_ips = new_ips

            channel_list = msg.get('channel_list', [])
            if channel_list:
                self.channel_list_updated.emit(channel_list)
            self.global_state_update.emit(all_users)

        elif act == 'channel_created':
            ch_name = msg.get('channel_name', '')
            if ch_name:
                self.channel_created.emit(ch_name)

        # FIX #4: сервер отвечает 'create_channel_result' после CMD_CREATE_CHANNEL.
        # Ранее network_engine не обрабатывал этот action → канал создавался на сервере,
        # но клиент-хост не получал уведомления → send_global_state присылал обновлённый
        # channel_list, но только если STATE DIRTY — что работало только косвенно.
        # Прямое решение: при ok=True эмитируем channel_created, чтобы UI обновился сразу.
        elif act == 'create_channel_result':
            if msg.get('ok'):
                ch_name = msg.get('channel_name', '')
                if ch_name:
                    self.channel_created.emit(ch_name)
            # ok=False — ошибки (not_host, invalid_name, already_exists) молча игнорируем:
            # сервер ничего не создал, UI ничего не обновляет. При необходимости
            # можно добавить сигнал create_channel_error в будущем.

        elif act == 'channel_deleted':
            ch_name = msg.get('channel_name', '')
            if ch_name:
                self.channel_deleted.emit(ch_name)

        elif act == 'join_room_denied':
            self.join_room_denied.emit(
                msg.get('room', ''),
                msg.get('reason', ''),
            )

        elif act == 'channel_auth_result':
            if msg.get('ok'):
                self.channel_auth_ok.emit(msg.get('channel_name', ''))
            else:
                # wrong_password или not_found → передаём как denied
                self.join_room_denied.emit(
                    msg.get('channel_name', ''),
                    msg.get('reason', 'wrong_password'),
                )

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

        # ── Быстрый чат ────────────────────────────────────────────────────
        elif act == CMD_QUICK_MSG:
            sender_uid  = int(msg.get('uid', 0))
            from_nick   = str(msg.get('from_nick', '?'))
            text        = str(msg.get('text', ''))
            if text:
                self.quick_msg_received.emit(sender_uid, from_nick, text)

        # ── Постоянный чат: входящее сообщение ────────────────────────────
        elif act == CMD_CHAT_MSG:
            sender_uid = int(msg.get('uid', 0))
            from_nick  = str(msg.get('from_nick', '?'))
            text       = str(msg.get('text', ''))
            avatar     = msg.get('avatar', '')
            ts         = float(msg.get('ts', time.time()))
            room       = str(msg.get('room', ''))
            if text:
                entry = {
                    'uid':    sender_uid,
                    'nick':   from_nick,
                    'avatar': avatar,
                    'text':   text,
                    'ts':     ts,
                    'room':   room,
                }
                self._chat_history.append(entry)
                if len(self._chat_history) > CHAT_HISTORY_MAX:
                    del self._chat_history[0]
                self.chat_msg_received.emit(entry)

        # ── Постоянный чат: история при подключении ───────────────────────
        elif act == CMD_CHAT_HISTORY:
            messages = msg.get('messages', [])
            if isinstance(messages, list) and messages:
                existing = {(m.get('uid', 0), m.get('ts', 0))
                            for m in self._chat_history}
                for m in messages:
                    key = (m.get('uid', 0), m.get('ts', 0))
                    if key not in existing:
                        self._chat_history.append(m)
                        existing.add(key)
                self._chat_history.sort(key=lambda m: m.get('ts', 0))
                if len(self._chat_history) > CHAT_HISTORY_MAX:
                    self._chat_history = self._chat_history[-CHAT_HISTORY_MAX:]
                self.chat_history_received.emit(list(self._chat_history))

        # ── Постоянный чат: медиа-вложение ────────────────────────────────────
        elif act == CMD_CHAT_MEDIA:
            entry = {
                'uid':          int(msg.get('uid', 0)),
                'nick':         str(msg.get('from_nick', '?')),
                'avatar':       msg.get('avatar', ''),
                'ts':           float(msg.get('ts', time.time())),
                'room':         str(msg.get('room', '')),
                'text':         '',
                'file_name':    msg.get('file_name', 'file'),
                'file_type':    msg.get('file_type', 'file'),
                'file_data_b64': msg.get('file_data_b64', ''),
            }
            if entry['file_data_b64']:
                self._chat_history.append(entry)
                if len(self._chat_history) > CHAT_HISTORY_MAX:
                    del self._chat_history[0]
                self.chat_media_received.emit(entry)

        # ── Постоянный чат: запрос истории (relay от сервера к хосту) ─────
        elif act == CMD_CHAT_HISTORY_REQ:
            requester_uid = int(msg.get('requester_uid', 0))
            if requester_uid and self._chat_history:
                history_slice = self._chat_history[-100:]
                self.send_json({
                    'action':     CMD_CHAT_HISTORY,
                    'target_uid': requester_uid,
                    'messages':   history_slice,
                })
                print(f"[Net] chat_history → uid={requester_uid}: "
                      f"{len(history_slice)} сообщений")

        # ── Хост выключил наш микрофон ─────────────────────────────────────
        elif act == CMD_FORCE_MUTED:
            self.force_muted.emit()

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
                # Мы — новый хост: сигнализируем UI запустить embedded server.
                # FIX (Bug F): устанавливаем running=False и _migration_pending=True
                # ДО emit, чтобы когда старый сервер разорвёт TCP-соединение,
                # tcp_listen увидел эти флаги и НЕ вызвал _on_connection_lost().
                # Без этого _on_connection_lost запускал _reconnect_loop параллельно
                # с fast_switch_to из _on_become_host → гонка → бесконечный цикл.
                self.running            = False
                self._migration_pending = True
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

    def send_quick_msg(self, text: str) -> None:
        """Отправить быстрое сообщение в комнату (≤ QUICK_MSG_MAX_LEN символов)."""
        text = text.strip()[:QUICK_MSG_MAX_LEN]
        if not text:
            return
        self.send_json({'action': CMD_QUICK_MSG, 'text': text})

    def send_chat_msg(self, text: str) -> None:
        """
        Отправить сообщение в постоянный чат комнаты (≤ CHAT_MSG_MAX_LEN символов).

        Сервер добавляет nick/uid/avatar/ts и рассылает всем в комнате.
        Отправитель тоже получает своё сообщение обратно от сервера —
        это гарантирует одинаковый порядок сообщений у всех участников.
        """
        text = text.strip()[:CHAT_MSG_MAX_LEN]
        if not text:
            return
        self.send_json({'action': CMD_CHAT_MSG, 'text': text})

    def send_chat_media(
        self, file_name: str, file_type: str, file_data_b64: str
    ) -> None:
        """
        Отправить медиа-вложение (фото, GIF, файл) в чат комнаты.
        file_type: 'image', 'gif', 'video', 'file'
        file_data_b64: содержимое файла в base64 (≤ CHAT_MEDIA_MAX_B64 символов)
        """
        if not file_data_b64 or len(file_data_b64) > CHAT_MEDIA_MAX_B64:
            print(f"[Net] send_chat_media: файл слишком большой или пустой")
            return
        self.send_json({
            'action':       CMD_CHAT_MEDIA,
            'file_name':    file_name,
            'file_type':    file_type,
            'file_data_b64': file_data_b64,
        })

    def send_host_mute(self, target_uid: int) -> None:
        """Хост выключает микрофон участника. Уши не трогаются. Участник может включить сам."""
        self.send_json({'action': CMD_HOST_MUTE, 'target_uid': int(target_uid)})

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
                # FIX: аналогично soundboard — внутренний микшер вместо sd.play()
                if hasattr(self.audio, 'play_internal_sound') and self.audio.stream:
                    self.audio.play_internal_sound(data, sr, 1.0)
                    time.sleep(len(data) / sr)
                else:
                    sd.play(data, sr)
                    sd.wait()
                print("[Nudge] Danger.mp3 воспроизведён успешно")
            except Exception as e:
                print(f"[Nudge] playback error: {e}")

        finally:
            self._nudge_restore_volume(prev_scalar, was_muted)