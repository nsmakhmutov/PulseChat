# network_engine/core.py — NetworkClient: основной сетевой клиент InPulse
#
# Фасад: объединяет WebRTCMixin, ChatMixin, FeaturesMixin.
# TCP/UDP сокеты, подключение, переподключение, миграция сервера.

import asyncio
import json
import platform
import socket
import struct
import threading
import time
import ctypes

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal

from config import (
    DEFAULT_PORT_TCP, DEFAULT_PORT_UDP, BUFFER_SIZE,
    UDP_HEADER_STRUCT, UDP_HEADER_SIZE,
    CMD_SOUNDBOARD, FLAG_STREAM_VOICES, FLAG_LOOPBACK_AUDIO,
    STREAM_VOICE_HEADER_STRUCT, STREAM_VOICE_HEADER_SIZE,
    FLAG_WHISPER,
    CMD_WEBRTC_OFFER, CMD_WEBRTC_ANSWER,
    CMD_SERVER_MIGRATE,
    CMD_CHAT_HISTORY_REQ,
    CHAT_HISTORY_MAX,
)

from .webrtc import WebRTCMixin
from .chat import ChatMixin
from .features import FeaturesMixin

MAX_SILENT_RECONNECT_ATTEMPTS = 2
RECONNECT_DELAY               = 1.0

# Точность таймера Windows
if platform.system() == "Windows":
    try:
        winmm = ctypes.WinDLL('winmm')
        winmm.timeBeginPeriod(1)
    except Exception:
        pass


class NetworkClient(WebRTCMixin, ChatMixin, FeaturesMixin, QObject):
    # ── Сигналы ───────────────────────────────────────────────────────────
    connected           = pyqtSignal(dict)
    global_state_update = pyqtSignal(dict)
    error_occurred      = pyqtSignal(str)

    connection_lost     = pyqtSignal()
    connection_restored = pyqtSignal()
    reconnect_failed    = pyqtSignal()

    soundboard_played   = pyqtSignal(str)

    nudge_received  = pyqtSignal()
    nudge_triggered = pyqtSignal(str, str)

    bitrate_adjusted = pyqtSignal(int)

    file_offer_received = pyqtSignal(dict)

    quick_msg_received  = pyqtSignal(int, str, str)

    chat_msg_received     = pyqtSignal(dict)
    chat_history_received = pyqtSignal(list)
    chat_media_received   = pyqtSignal(dict)

    become_host      = pyqtSignal()
    server_migrating = pyqtSignal(str)

    channel_created      = pyqtSignal(str)
    channel_deleted      = pyqtSignal(str)
    join_room_denied     = pyqtSignal(str, str)
    channel_auth_ok      = pyqtSignal(str)
    channel_list_updated = pyqtSignal(list)

    force_muted = pyqtSignal()

    draw_stroke_received = pyqtSignal(int, str, str, list, int)

    # Typing indicator: (uid, nick) — кто-то печатает в комнате
    typing_received = pyqtSignal(int, str)

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

        self._sb_playing = threading.Event()

        self._chat_history: list[dict] = []

        # ── Встроенный сервер: миграция ──────────────────────────────────
        self._host_order:       list[int]      = []
        self._server_host_uid:  int            = 0
        self._migration_pending: bool          = False
        self._host_order_ips:   dict[int, str] = {}

        # ── Инициализация WebRTC атрибутов из миксина ────────────────────
        self._init_webrtc_attrs()

        self._init_sockets()

    # ------------------------------------------------------------------
    # Сокеты
    # ------------------------------------------------------------------
    def _init_sockets(self):
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
            self.tcp_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_socket_bound = False
        except Exception as e:
            print(f"[Net] Socket init error: {e}")

    # ------------------------------------------------------------------
    # Подключение
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

        self._start_webrtc_loop()

        # media-engine.exe и SFU запускаются лениво:
        # только при start_streaming_webrtc() когда пользователь начинает стрим.
        # Зрителям media-engine не нужен — они используют aiortc viewer PC.

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

        # FIX: закрываем viewer PC до reconnect.
        # Старый _viewer_pc привязан к упавшему ICE — новый handshake поверх него
        # невозможен. Без явного close() Pion SFU продолжает слать PLI
        # по мёртвому треку что создаёт PLI-шторм в Rust pipeline.
        if self._viewer_pc is not None:
            try:
                self._run_in_webrtc_loop(self._close_pc_coro(self._viewer_pc))
            except Exception:
                pass
            self._viewer_pc = None
        # Останавливаем audio playback зрителя (иначе старый буфер продолжает играть)
        if self.audio is not None and hasattr(self.audio, 'stop_stream_playback'):
            try:
                self.audio.stop_stream_playback()
            except Exception:
                pass

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

        if self._host_order:
            threading.Thread(
                target=self._auto_host_check, daemon=True, name="auto-host"
            ).start()

    def manual_reconnect(self):
        if self._reconnecting:
            return
        if not self._ip:
            return
        self._reconnecting       = True
        self._reconnect_attempts = 0
        self.running = False
        threading.Thread(target=self._reconnect_loop, daemon=True).start()

    def fast_switch_to(self, new_ip: str) -> None:
        if self._reconnecting:
            return
        self._ip             = new_ip
        self.running         = False
        self._reconnecting   = True
        self._reconnect_attempts = 0
        threading.Thread(
            target=self._fast_switch_loop, daemon=True, name="fast-switch",
        ).start()

    def _fast_switch_loop(self) -> None:
        MAX_ATTEMPTS = 8
        FAST_DELAY   = 0.35
        for attempt in range(1, MAX_ATTEMPTS + 1):
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

        self._reconnecting = False
        self.reconnect_failed.emit()

    # ------------------------------------------------------------------
    # Миграция
    # ------------------------------------------------------------------
    def _migrate_reconnect(self) -> None:
        if not self._migration_pending:
            return
        self.running = False
        self._migration_pending = False
        self._reconnecting      = False
        self.fast_switch_to(self._ip)

    def _auto_host_check(self) -> None:
        try:
            my_uid   = getattr(self.audio, 'my_uid', 0)
            old_host = self._server_host_uid
            remaining_order = [uid for uid in self._host_order if uid != old_host]

            if not remaining_order:
                self.become_host.emit()
                return

            try:
                my_pos = remaining_order.index(my_uid)
            except ValueError:
                first_ip = self._host_order_ips.get(remaining_order[0], '')
                if first_ip:
                    self._wait_and_connect_group(first_ip)
                return

            if my_pos == 0:
                self.become_host.emit()
                return

            wait_sec = my_pos * 1.5
            time.sleep(wait_sec)

            for candidate_uid in remaining_order[:my_pos]:
                candidate_ip = self._host_order_ips.get(candidate_uid, '')
                if not candidate_ip:
                    continue
                if self._try_group_connect(candidate_ip):
                    return

            self.become_host.emit()

        except Exception as e:
            print(f"[Net] _auto_host_check error: {e}")

    def _try_group_connect(self, ip: str, max_attempts: int = 6) -> bool:
        import socket as _sock
        for attempt in range(1, max_attempts + 1):
            try:
                s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
                s.settimeout(1.0)
                s.connect((ip, DEFAULT_PORT_TCP))
                s.close()
                self._reconnecting = False
                self.fast_switch_to(ip)
                return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def _wait_and_connect_group(self, ip: str) -> None:
        if self._try_group_connect(ip, max_attempts=30):
            return
        self.reconnect_failed.emit()

    # ------------------------------------------------------------------
    # Остановка
    # ------------------------------------------------------------------
    def stop(self) -> None:
        print("[Net] stop(): завершаем сетевые потоки...")
        self.running = False
        self._is_connected = False

        self._stop_webrtc()

        for attr in ('tcp_sock', 'udp_sock'):
            sock = getattr(self, attr, None)
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

        print("[Net] stop(): готово")

    def send_server_transfer(self, target_uid: int) -> None:
        self.send_json({'action': 'server_transfer', 'target_uid': target_uid})

    # ------------------------------------------------------------------
    # UDP
    # ------------------------------------------------------------------
    def udp_receive_loop(self):
        while self.running:
            try:
                data, addr = self.udp_sock.recvfrom(BUFFER_SIZE)
                if len(data) < UDP_HEADER_SIZE:
                    continue

                uid, ts, seq, flags = UDP_HEADER_STRUCT.unpack(data[:UDP_HEADER_SIZE])

                if flags == 254:
                    self.packets_received += 1
                    delay = (time.time() - ts) * 1000
                    if self.current_ping == 0:
                        self.current_ping = int(delay)
                    else:
                        self.current_ping = int(self.current_ping * 0.7 + delay * 0.3)

                elif flags & FLAG_STREAM_VOICES:
                    if len(data) < UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE:
                        continue
                    speaker_uid, = STREAM_VOICE_HEADER_STRUCT.unpack(
                        data[UDP_HEADER_SIZE: UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE]
                    )
                    if speaker_uid == self.audio.my_uid:
                        continue
                    opus_payload = data[UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE:]
                    self.audio.add_incoming_packet(speaker_uid, seq, opus_payload, flags)

                elif flags & FLAG_WHISPER:
                    if len(data) < UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE:
                        continue
                    opus_payload = data[UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE:]
                    self.audio.add_incoming_whisper_packet(uid, seq, opus_payload)

                else:
                    self.audio.add_incoming_packet(uid, seq, data[UDP_HEADER_SIZE:], flags)

            except OSError as e:
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

    def udp_sender_loop(self):
        while self.running:
            try:
                packet = self.audio.send_queue.get(timeout=0.1)
                if self.server_addr:
                    self.udp_sock.sendto(packet, self.server_addr)
            except Exception:
                continue

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

                if self.current_ping > 0:
                    try:
                        self.send_json({'action': 'report_ping', 'ping_ms': self.current_ping})
                    except Exception:
                        pass
            time.sleep(3)

    # ------------------------------------------------------------------
    # TCP
    # ------------------------------------------------------------------
    def tcp_listen(self):
        raw_data = ""
        _decoder = json.JSONDecoder()
        _RAW_DATA_MAX = 32 * 1024 * 1024
        while self.running:
            try:
                chunk_bytes = self.tcp_sock.recv(4096)
                if not chunk_bytes:
                    break
                raw_data += chunk_bytes.decode('utf-8', errors='ignore')
                if len(raw_data) > _RAW_DATA_MAX:
                    print(f"[Net] WARN: raw_data overflow ({len(raw_data)} bytes) — clearing")
                    raw_data = ""
                while True:
                    try:
                        msg, idx = _decoder.raw_decode(raw_data)
                        raw_data = raw_data[idx:].lstrip()
                        try:
                            self.process_message(msg)
                        except Exception as pm_err:
                            import traceback
                            print(f"[Net] process_message error: {pm_err}\n"
                                  f"{traceback.format_exc()}")
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
        if self.running and not self._migration_pending:
            self._on_connection_lost()

    def process_message(self, msg: dict):
        act = msg.get('action')

        # ── Core messages ─────────────────────────────────────────────────
        if act == 'login_success':
            self.connected.emit(msg)
            self.send_json({'action': CMD_CHAT_HISTORY_REQ})

        elif act == 'sync_users':
            self._host_order      = msg.get('host_order', [])
            self._server_host_uid = msg.get('server_host_uid', 0)

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

        elif act == 'create_channel_result':
            if msg.get('ok'):
                ch_name = msg.get('channel_name', '')
                if ch_name:
                    self.channel_created.emit(ch_name)

        elif act == 'channel_deleted':
            ch_name = msg.get('channel_name', '')
            if ch_name:
                self.channel_deleted.emit(ch_name)

        elif act == 'join_room_denied':
            self.join_room_denied.emit(msg.get('room', ''), msg.get('reason', ''))

        elif act == 'channel_auth_result':
            if msg.get('ok'):
                self.channel_auth_ok.emit(msg.get('channel_name', ''))
            else:
                self.join_room_denied.emit(
                    msg.get('channel_name', ''),
                    msg.get('reason', 'wrong_password'),
                )

        # ── Миграция ─────────────────────────────────────────────────────
        elif act == CMD_SERVER_MIGRATE:
            new_host_uid = msg.get('new_host_uid', 0)
            new_host_ip  = msg.get('new_host_ip', '')
            my_uid = getattr(self.audio, 'my_uid', 0)
            if new_host_uid == my_uid:
                self.running            = False
                self._migration_pending = True
                self.become_host.emit()
            elif new_host_ip:
                self._ip = new_host_ip
                self._migration_pending = True
                self.server_migrating.emit(new_host_ip)
                threading.Thread(
                    target=self._migrate_reconnect, daemon=True, name="net-migrate",
                ).start()

        # ── Делегирование в миксины ──────────────────────────────────────
        elif self._process_webrtc_message(msg, act):
            pass
        elif self._process_chat_message(msg, act):
            pass
        elif self._process_features_message(msg, act):
            pass
        # Неизвестные actions тихо игнорируются

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
        self.send_json({
            "action":      "update_presence",
            "status_icon": status_icon,
            "status_text": status_text,
        })

    def set_video_engine(self, video) -> None:
        self.video = video
        if self._webrtc_loop is not None and not self._webrtc_loop.is_closed():
            video.set_webrtc_loop(self._webrtc_loop)
        print("[Net] VideoEngine registered")