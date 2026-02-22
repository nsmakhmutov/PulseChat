import socket
import threading
import json
import time
import pygame
import struct
import os
from PyQt6.QtCore import QObject, pyqtSignal, QSettings

from config import (
    resource_path, DEFAULT_PORT_TCP, DEFAULT_PORT_UDP, BUFFER_SIZE,
    UDP_HEADER_STRUCT, UDP_HEADER_SIZE, FLAG_VIDEO, FLAG_STREAM_AUDIO, MAX_VIDEO_PAYLOAD,
    CMD_SOUNDBOARD, FLAG_LOOPBACK_AUDIO, FLAG_STREAM_VOICES,
    STREAM_VOICE_HEADER_STRUCT, STREAM_VOICE_HEADER_SIZE
)

MAX_SILENT_RECONNECT_ATTEMPTS = 4
RECONNECT_DELAY = 3.0


class NetworkClient(QObject):
    connected = pyqtSignal(dict)
    global_state_update = pyqtSignal(dict)
    error_occurred = pyqtSignal(str)

    connection_lost = pyqtSignal()
    connection_restored = pyqtSignal()
    reconnect_failed = pyqtSignal()

    def __init__(self, audio):
        super().__init__()
        self.audio = audio
        self.video = None
        self.server_addr = None
        self.running = False
        self.current_ping = 0
        self.packets_sent = 0
        self.packets_received = 0

        self._ip = None
        self._nick = None
        self._avatar = None

        self._is_connected = False
        self._reconnecting = False
        self._reconnect_attempts = 0

        self._init_sockets()

    def _init_sockets(self):
        try:
            self.tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_socket_bound = False
        except Exception as e:
            print(f"[Net] Socket init error: {e}")

    def play_soundboard_file(self, filename):
        try:
            path = resource_path(os.path.join("assets/panel", filename))
            if os.path.exists(path):
                sound = pygame.mixer.Sound(path)
                vol = int(QSettings("MyVoiceChat", "GlobalSettings").value("soundboard_volume", 50)) / 100.0
                sound.set_volume(vol)
                sound.play()
                print(f"[Net] Playing soundboard: {filename}")
            else:
                print(f"[Net] Soundboard file not found: {path}")
        except Exception as e:
            print(f"[Net] Soundboard error: {e}")

    def connect_to_server(self, ip, nick, avatar):
        self._ip = ip
        self._nick = nick
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

        try:
            self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            print("[Net] UDP receive buffer set to 1MB")
        except Exception:
            pass

        self.send_json({"action": "login", "nick": self._nick, "avatar": self._avatar})
        self.running = True
        self._is_connected = True

        threading.Thread(target=self.tcp_listen, daemon=True).start()
        threading.Thread(target=self.udp_sender_loop, daemon=True).start()
        threading.Thread(target=self.udp_keepalive_loop, daemon=True).start()
        threading.Thread(target=self.udp_receive_loop, daemon=True).start()
        threading.Thread(target=self.ping_loop, daemon=True).start()

        print("[Net] Connected to server")

    def _on_connection_lost(self):
        if self._reconnecting:
            return
        self._reconnecting = True
        self._is_connected = False
        self.running = False
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

                self._reconnecting = False
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
        self._reconnecting = True
        self._reconnect_attempts = 0
        self.running = False

        threading.Thread(target=self._reconnect_loop, daemon=True).start()

    def send_video_packet(self, payload):
        if not self.server_addr or self.audio.my_uid == 0:
            return
        header = UDP_HEADER_STRUCT.pack(self.audio.my_uid, time.time(), 0, FLAG_VIDEO)
        packet = header + payload
        try:
            self.udp_sock.sendto(packet, self.server_addr)
            self.packets_sent += 1
        except Exception as e:
            print(f"[Net] Error sending video packet: {e}")

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

                elif flags & FLAG_VIDEO:
                    if self.video:
                        self.video.process_incoming_packet(uid, data[UDP_HEADER_SIZE:])
                    else:
                        print(f"[Net] Video packet from {uid}, but VideoEngine not initialized")
                elif flags & FLAG_STREAM_AUDIO and flags & FLAG_STREAM_VOICES:
                    # Голосовой поток стрима (Mix Minus).
                    # Payload: [speaker_uid: 4 байта] + [opus данные].
                    # Отбрасываем пакет если speaker_uid == наш собственный uid —
                    # так зритель не слышит своего голоса в стриме без DSP.
                    if len(data) < UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE:
                        continue
                    speaker_uid, = STREAM_VOICE_HEADER_STRUCT.unpack(
                        data[UDP_HEADER_SIZE: UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE]
                    )
                    if speaker_uid == self.audio.my_uid:
                        # Свой голос — тихо отбрасываем (Mix Minus без DSP)
                        continue
                    # Чужой голос — передаём с speaker_uid (не uid стримера!).
                    # Это критично: если передавать uid стримера, все голоса разных
                    # спикеров попадают в один jitter buffer stream_remote_users[streamer]
                    # и вытесняют друг друга по seq. С speaker_uid каждый спикер
                    # получает отдельный слот → нет конфликтов seq.
                    # Фильтр recently_received в _stream_packet_processor_loop работает
                    # корректно: если viewer в той же комнате — speaker есть в remote_users
                    # → пакет отброшен (уже слышим напрямую). Если в другой — играет.
                    opus_payload = data[UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE:]
                    self.audio.add_incoming_stream_packet(speaker_uid, seq, opus_payload, flags)

                elif flags & FLAG_STREAM_AUDIO:
                    # Стрим-аудио: отфильтровка и воспроизведение — в AudioHandler
                    is_loopback = bool(flags & FLAG_LOOPBACK_AUDIO)
                    if seq % 50 == 0:
                        print(f"[Net-Recv] Получен пакет FLAG_STREAM_AUDIO (loopback={is_loopback}) от uid={uid}")
                    self.audio.add_incoming_stream_packet(uid, seq, data[UDP_HEADER_SIZE:], flags)
                else:
                    self.audio.add_incoming_packet(uid, seq, data[UDP_HEADER_SIZE:], flags)
            except Exception as e:
                if self.running:
                    print(f"[Net] UDP receive error: {e}")
                continue

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

    def tcp_listen(self):
        raw_data = ""
        while self.running:
            try:
                # Исправление 1.1: Читаем сырые байты, игнорируем разорванные UTF-8 символы
                chunk_bytes = self.tcp_sock.recv(4096)
                if not chunk_bytes:
                    print("[Net] Server closed connection (empty recv).")
                    break
                raw_data += chunk_bytes.decode('utf-8', errors='ignore')

                while True:
                    try:
                        msg, idx = json.JSONDecoder().raw_decode(raw_data)
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
        self.send_json({
            "action": "update_status",
            "mute": mute,
            "deaf": deaf
        })

    def set_video_engine(self, video):
        self.video = video
        print("[Net] VideoEngine registered")