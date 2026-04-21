# server.py — SFUServer + EmbeddedServerManager
#
# ── Полный список исправлений v2 ────────────────────────────────────────────
#
#   FIX #1 (КРИТИЧНО): stream_watch_start — UnboundLocalError при гонке
#     "клиент отключился во время обработки". w_uid/watcher_nick/watcher_avatar
#     защищены проверкой conn in clients.
#
#   FIX #2 (ВАЖНО): stats атомарность — отдельный stats_lock вместо ложной
#     надежды на GIL. `dict[key] += n` НЕ атомарно в CPython (три байткода).
#
#   FIX #3 (ВАЖНО): аннотация _media_cache исправлена на 5-элементный тупл.
#
#   FIX #4 (ВАЖНО): UTF-8 incremental decoder для TCP stream. Раньше
#     errors='ignore' обрезал байты эмоджи/кириллицы на границе chunk.
#
#   FIX #5 (ВАЖНО): _state_dirty под локом (clients_lock). Раньше два потока
#     могли потерять dirty-флаг.
#
#   FIX #6 (ВАЖНО): _channel_auth очищается при rename_channel.
#
#   FIX #7 (ВАЖНО): сравнение с _general_channel_name вместо хардкод 'General'.
#
#   FIX #8 (СТИЛЬ): `except Exception` вместо `except (ValueError, Exception)`.
#
#   FIX #9 (СТИЛЬ): math.isfinite() для ping_ms (защита от nan/inf).
#
#   FIX #10 (СТИЛЬ): структурированный except в start_embedded SFU-бриджа.

import asyncio
import codecs
import hashlib
import json
import math
import os
import queue as _queue
import secrets
import socket
import threading
import time

from config import (
    DEFAULT_PORT_TCP, DEFAULT_PORT_UDP, BUFFER_SIZE,
    UDP_RECV_BUFFER_SIZE, UDP_SEND_BUFFER_SIZE,
    UDP_HEADER_STRUCT, UDP_HEADER_SIZE,
    FLAG_STREAM_VOICES, FLAG_WHISPER, FLAG_ANONYMOUS,
    STREAM_VOICE_HEADER_STRUCT, STREAM_VOICE_HEADER_SIZE,
    ANONYMOUS_UID,
    CMD_LOGIN, CMD_JOIN_ROOM, CMD_STREAM_START, CMD_STREAM_STOP,
    CMD_SYNC_USERS, CMD_SOUNDBOARD,
    CMD_UPDATE_PRESENCE,
    CMD_NUDGE_VOTE, CMD_PLAY_NUDGE, CMD_NUDGE_TRIGGERED, NUDGE_COOLDOWN_SEC,
    CMD_FILE_OFFER, CMD_FILE_OFFER_ROOM,
    CMD_WEBRTC_OFFER, CMD_WEBRTC_ANSWER, CMD_WEBRTC_ICE,
    CMD_SERVER_TRANSFER, CMD_SERVER_MIGRATE,
    CMD_MIGRATE_PREPARE, CMD_MIGRATE_READY, MIGRATE_PREPARE_TIMEOUT_SEC,
    CMD_QUICK_MSG, QUICK_MSG_MAX_LEN,
    CMD_CREATE_CHANNEL, CMD_CHANNEL_CREATED, CMD_CHANNEL_DELETED,
    CMD_JOIN_CHANNEL_AUTH, CHANNEL_NAME_MAX_LEN, CHANNEL_PASS_MAX_LEN,
    SERVER_NAME_DEFAULT,
    CMD_HOST_MUTE, CMD_FORCE_MUTED,
    CMD_CHAT_MSG, CMD_CHAT_HISTORY, CMD_CHAT_HISTORY_REQ, CHAT_MSG_MAX_LEN,
    CMD_CHAT_MEDIA, CHAT_MEDIA_MAX_B64,
    CMD_DRAW_STROKE, DRAW_MAX_POINTS,
    CMD_TYPING, CHAT_DB_PATH, CHAT_HISTORY_MAX,
)
from .chat_db import ChatDB
from .server_webrtc import PionSfuProxy


# FIX #4: максимальный размер входного буфера на одного клиента.
# При CHAT_MEDIA до 10 MB в base64 + overhead JSON, 16 MB — безопасный потолок.
# Если буфер растёт сверх этого — клиент либо атакует, либо что-то сломано.
_TCP_BUFFER_MAX = 16 * 1024 * 1024


# =============================================================================
# SFUServer — основной сервер
# =============================================================================

class SFUServer:
    def __init__(self, host='0.0.0.0', server_name=''):
        # --- TCP ---
        self.tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp_sock.bind((host, DEFAULT_PORT_TCP))
        self.tcp_sock.listen()

        # --- UDP ---
        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, UDP_RECV_BUFFER_SIZE)
        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, UDP_SEND_BUFFER_SIZE)
        self.udp_sock.bind((host, DEFAULT_PORT_UDP))

        # -------------------------------------------------------------------
        # Разделение локов:
        #   clients_lock  — self.clients (TCP-потоки)
        #   udp_lock      — self.udp_map (UDP-поток)
        #   watchers_lock — self.watchers (stream-события)
        # -------------------------------------------------------------------
        self.clients_lock  = threading.Lock()
        self.udp_lock      = threading.Lock()
        self.watchers_lock = threading.Lock()

        self.clients = {}
        self.udp_map = {}
        self.uid_to_room = {}
        self.watchers = {}

        # FIX #2: stats не атомарны через GIL, нужен отдельный лок.
        # `self.stats["packets"] += 1` = LOAD + ADD + STORE — три байткода.
        # Между ними GIL может переключиться → потери счётчика.
        self.stats      = {"packets": 0, "bytes": 0}
        self.stats_lock = threading.Lock()
        self.start_time = time.time()

        # --- Голосование «Пнуть» (Nudge) ---
        self.nudge_votes = {}
        self.nudge_lock  = threading.Lock()

        # --- WebRTC SFU ---
        self.sfu: PionSfuProxy | None = None

        # ── Кэш payload send_global_state ───────────────────────────────────
        self._cached_payload: bytes | None = None
        self._state_dirty: bool = True

        # ── Встроенный сервер: host_order ────────────────────────────────────
        self._host_order:      list[int] = []
        self._host_order_lock            = threading.Lock()

        # ── Встроенный сервер: управление ────────────────────────────────────
        self._is_embedded: bool = False
        self._accepting:   bool = True
        self._announcer         = None
        self._owner_ip:    str  = ''

        # ── Имя сервера ──────────────────────────────────────────────────────
        self._server_name = server_name or SERVER_NAME_DEFAULT

        # ── Пинги клиентов ────────────────────────────────────────────────────
        self._client_pings: dict[int, int] = {}
        self._client_pings_lock = threading.Lock()

        # ── 2-шаговая ручная передача сервера ──
        self._pending_migrate_target: int | None             = None
        self._pending_migrate_ready:  threading.Event | None = None
        self._pending_migrate_lock:   threading.Lock         = threading.Lock()

        # ── Временные каналы ─────────────────────────────────────────────────
        _general_name = 'General'
        try:
            import json as _json_cfg
            from config import USER_CONFIG_PATH as _UCP
            if os.path.exists(_UCP):
                with open(_UCP, 'r', encoding='utf-8') as _f:
                    _general_name = _json_cfg.load(_f).get('general_channel_name', 'General') or 'General'
        except Exception:
            pass
        self._general_channel_name: str = _general_name
        self._channels: dict = {
            _general_name: {'password': None, 'permanent': True},
        }
        self._channels_lock = threading.Lock()

        # Кэш авторизованных клиентов
        self._channel_auth: dict = {}
        self._channel_auth_lock = threading.Lock()

        # ── Кэш медиа-пейлоадов ──────────────────────────────────────────────
        # SENIOR FIX: раньше хранили prefix/suffix разрезанный по байтам ts,
        # что было хрупко (зависело от float-repr). Теперь prefix — полный JSON
        # без закрывающей '}', suffix всегда пустой (legacy slot — чтобы
        # не менять 5-tuple схему). При отправке дописываем ',"ts":<ts>}'.
        self._media_cache: dict[str, tuple[float, bytes, bytes, int, str]] = {}
        self._media_cache_lock = threading.Lock()
        self._media_cache_max  = 30
        self._media_cache_ttl  = 300.0

        # ── SQLite чат ────────────────────────────────────────────────────────
        self._chat_db: ChatDB | None = None
        try:
            self._chat_db = ChatDB()
            print(f"[Server] SQLite чат: {CHAT_DB_PATH}")
        except Exception as e:
            print(f"[Server] ChatDB init error: {e}")
            self._chat_db = None

        # FIX: send_global_state broadcast queue.
        # Раньше sync_users рассылался в цикле СИНХРОННО из tcp_handler-потока.
        # Если у одного клиента заполнен TCP send-буфер, sendall блокируется и
        # задерживает рассылку всем остальным (и сам tcp_handler тоже встаёт).
        # Решение: выделенный поток-broadcaster с очередью.
        # tcp_handler только кладёт (payload, conns) в очередь и немедленно
        # возвращается — медленный клиент изолирован в broadcaster-потоке.
        self._bcast_queue: _queue.Queue = _queue.Queue(maxsize=64)
        self._bcast_thread = threading.Thread(
            target=self._bcast_loop, daemon=True, name="srv-bcast"
        )
        self._bcast_thread.start()

    # ------------------------------------------------------------------
    # Broadcaster loop
    # ------------------------------------------------------------------
    def _bcast_loop(self) -> None:
        """
        Выделенный поток рассылки sync_users.

        Берёт (payload, conns) из очереди и отправляет каждому клиенту.
        Медленный клиент замедляет только свою отправку; остальные не ждут.

        SO_SNDTIMEO = 500 мс: если TCP send-буфер клиента не освобождается
        за 500 мс — sendall завершается с ошибкой (не с зависанием).
        Следующий вызов send_global_state обновит состояние повторно.
        Только для sendall-вызовов из broadcaster — recv в tcp_handler
        использует отдельный таймаут и не затронут.
        """
        from .server_webrtc import _get_conn_lock
        while self._accepting:
            try:
                item = self._bcast_queue.get(timeout=1.0)
            except _queue.Empty:
                continue
            payload, conns = item
            for conn in conns:
                try:
                    lock = _get_conn_lock(conn)
                    with lock:
                        # SO_SNDTIMEO: на Windows принимает int (миллисекунды).
                        # На других ОС — struct timeval, но приложение Windows-only.
                        try:
                            conn.setsockopt(
                                socket.SOL_SOCKET, socket.SO_SNDTIMEO, 500
                            )
                        except Exception:
                            pass
                        try:
                            conn.sendall(payload)
                        except Exception:
                            pass
                        try:
                            conn.setsockopt(
                                socket.SOL_SOCKET, socket.SO_SNDTIMEO, 0
                            )
                        except Exception:
                            pass
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Управление каналами
    # ------------------------------------------------------------------
    def get_client_count(self) -> int:
        with self.clients_lock:
            return len(self.clients)

    def _get_user_nicks_list(self) -> list:
        with self.clients_lock:
            return [c.get('nick', '') for c in self.clients.values() if c.get('nick')]

    def _pick_best_host(self, exclude_uid: int = 0) -> tuple[int, str]:
        """Выбирает лучшего кандидата в новые хосты по минимальному RTT."""
        with self.clients_lock:
            candidates = [
                (c['uid'], c.get('ip', ''))
                for c in self.clients.values()
                if c['uid'] != exclude_uid and c.get('ip')
            ]

        if not candidates:
            return 0, ''

        with self._client_pings_lock:
            pings_snapshot = dict(self._client_pings)

        with self._host_order_lock:
            order_snapshot = list(self._host_order)

        def _sort_key(item):
            uid, _ = item
            ping = pings_snapshot.get(uid, 999)
            try:
                pos = order_snapshot.index(uid)
            except ValueError:
                pos = 9999
            return (ping, pos)

        best_uid, best_ip = min(candidates, key=_sort_key)
        best_ping = pings_snapshot.get(best_uid, -1)
        print(
            f"[Server] _pick_best_host: лучший кандидат uid={best_uid} "
            f"ip={best_ip} ping={best_ping}ms"
        )
        return best_uid, best_ip

    def _get_channel_list(self) -> list:
        with self._channels_lock:
            return [
                {
                    'name':         name,
                    'has_password': ch['password'] is not None,
                    'permanent':    ch['permanent'],
                }
                for name, ch in self._channels.items()
            ]

    def _create_temp_channel(self, name: str, password) -> bool:
        with self._channels_lock:
            if name in self._channels:
                return False
            self._channels[name] = {'password': password, 'permanent': False}
        print(f"[Server] 📢 Создан канал '{name}' (пароль: {'да' if password else 'нет'})")
        return True

    def _cleanup_temp_channels(self, leaving_room: str):
        """Удаляет пустой временный канал и рассылает CMD_CHANNEL_DELETED.

        SENIOR FIX: TOCTOU-гонка. Раньше occupants-проверка и pop() были
        в разных lock-секциях: между ними новый клиент мог войти в канал,
        попадая в "фантомный" канал (удалённый, но кто-то внутри).

        Решение: финальная проверка occupants под ОБОИМИ локами одновременно
        перед pop(). Порядок локов: channels_lock → clients_lock (согласован
        с другими местами в коде).
        """
        # Быстрый exit: канал постоянный или не существует
        with self._channels_lock:
            ch = self._channels.get(leaving_room)
            if not ch or ch['permanent']:
                return

        with self.clients_lock:
            occupants = sum(
                1 for c in self.clients.values()
                if c.get('room') == leaving_room
            )
        if occupants > 0:
            return

        # SENIOR FIX: атомарная финальная проверка + pop под channels_lock+clients_lock
        with self._channels_lock:
            ch = self._channels.get(leaving_room)
            if not ch or ch['permanent']:
                return
            # Перепроверяем occupants — если кто-то вошёл между двумя проверками
            with self.clients_lock:
                occupants_final = sum(
                    1 for c in self.clients.values()
                    if c.get('room') == leaving_room
                )
                if occupants_final > 0:
                    return
                self._channels.pop(leaving_room, None)

        # FIX #6: чистим _channel_auth для удалённого канала у всех клиентов.
        with self._channel_auth_lock:
            for auth_set in self._channel_auth.values():
                auth_set.discard(leaving_room)

        print(f"[Server] 🗑 Временный канал '{leaving_room}' удалён (пустой)")
        payload = json.dumps({
            'action':       CMD_CHANNEL_DELETED,
            'channel_name': leaving_room,
        }).encode('utf-8')
        with self.clients_lock:
            conns = list(self.clients.keys())
        # SENIOR FIX: _safe_send вместо прямого sendall — защита от гонки
        # с broadcaster-потоком (_bcast_loop шлёт sync_users параллельно).
        for c in conns:
            self._safe_send(c, payload)

    def _check_channel_auth(self, conn, channel_name: str) -> bool:
        with self._channels_lock:
            ch = self._channels.get(channel_name)
        if ch is None:
            return False
        if ch['password'] is None:
            return True
        with self._channel_auth_lock:
            return channel_name in self._channel_auth.get(conn, set())

    def send_to_conn(self, conn, msg: dict) -> None:
        """Синхронная отправка JSON клиенту."""
        self._safe_send(conn, json.dumps(msg).encode('utf-8'))

    def _safe_send(self, conn, payload: bytes) -> None:
        """
        Потокобезопасная отправка байтов клиенту.

        FIX #47 (серверная сторона): sendall() на один socket из разных потоков
        (например, broadcast из tcp_handler и ответ на команду из другого) мог
        перемешать байты JSON. Используем per-conn lock из server_webrtc.
        """
        from .server_webrtc import _get_conn_lock
        lock = _get_conn_lock(conn)
        with lock:
            try:
                conn.sendall(payload)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Мониторинг
    # ------------------------------------------------------------------
    def stats_monitor(self):
        last_bytes = 0
        while self._accepting:
            time.sleep(5)
            if not self._accepting:
                break
            with self.clients_lock:
                active = len(self.clients)
            # FIX #2: читаем под локом для консистентности.
            with self.stats_lock:
                curr_bytes = self.stats["bytes"]
            diff = (curr_bytes - last_bytes) / 1024 / 5
            print(f"[Stats] Active: {active} | Traffic: {diff:.1f} KB/s")
            last_bytes = curr_bytes

    # ------------------------------------------------------------------
    # UDP-маршрутизация
    # ------------------------------------------------------------------
    def udp_handler(self):
        """UDP-поток держит лок только на минимальное время."""
        while self._accepting:
            try:
                data, addr = self.udp_sock.recvfrom(BUFFER_SIZE)
                if len(data) < UDP_HEADER_SIZE:
                    continue

                sender_uid, msg_ts, seq, flags = UDP_HEADER_STRUCT.unpack(
                    data[:UDP_HEADER_SIZE]
                )

                # Ping: отвечаем немедленно
                if flags == 254:
                    self.udp_sock.sendto(data, addr)
                    continue

                with self.udp_lock:
                    self.udp_map[sender_uid] = addr
                    sender_room = self.uid_to_room.get(sender_uid)
                # FIX #2: stats_lock защищает от race
                with self.stats_lock:
                    self.stats["packets"] += 1
                    self.stats["bytes"]   += len(data)

                if not sender_room:
                    continue

                is_stream_voices = bool(flags & FLAG_STREAM_VOICES)
                is_whisper       = bool(flags & FLAG_WHISPER)

                if is_whisper:
                    if len(data) < UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE:
                        continue
                    (target_uid,) = STREAM_VOICE_HEADER_STRUCT.unpack(
                        data[UDP_HEADER_SIZE: UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE]
                    )
                    with self.udp_lock:
                        target_addr = self.udp_map.get(target_uid)
                    if target_addr:
                        # ── Анонимный шёпот ──────────────────────────────────
                        # Клиент-отправитель не может сам подменить sender_uid
                        # в header (иначе сервер не найдёт sender_room и дропнет
                        # пакет). Поэтому подмену делает сервер: сохраняем
                        # keepalive по реальному sender_uid, но на wire к
                        # получателю отправляем пакет с ANONYMOUS_UID в header.
                        # Реальный uid отправителя остаётся известен ТОЛЬКО
                        # серверу — получатель физически его не видит.
                        if flags & FLAG_ANONYMOUS:
                            new_header = UDP_HEADER_STRUCT.pack(
                                ANONYMOUS_UID, msg_ts, seq, flags
                            )
                            out_data = new_header + data[UDP_HEADER_SIZE:]
                        else:
                            out_data = data
                        try:
                            self.udp_sock.sendto(out_data, target_addr)
                        except Exception:
                            pass

                elif is_stream_voices:
                    self._send_to_watchers(sender_uid, data)

                else:
                    # АУДИО КОМНАТЫ
                    with self.clients_lock:
                        target_uids = [
                            c_data['uid']
                            for c_data in self.clients.values()
                            if c_data['uid'] != sender_uid
                               and c_data['room'] == sender_room
                        ]

                    with self.udp_lock:
                        target_addrs = [
                            self.udp_map[uid]
                            for uid in target_uids
                            if uid in self.udp_map
                        ]

                    for target_addr in target_addrs:
                        try:
                            self.udp_sock.sendto(data, target_addr)
                        except Exception:
                            pass

            except OSError:
                break
            except Exception:
                if not self._accepting:
                    break

    # ------------------------------------------------------------------
    # TCP-обработчик одного клиента
    # ------------------------------------------------------------------
    def tcp_handler(self, conn, addr):
        uid       = secrets.randbelow(10**9) + 1
        client_ip = addr[0]
        _decoder  = json.JSONDecoder()

        # FIX #4: инкрементальный UTF-8 декодер корректно обрабатывает байты
        # на границах chunk. Раньше `bytes.decode(errors='ignore')` съедал
        # частичные последовательности эмоджи/кириллицы на границе recv(4096).
        utf8_decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        buffer = ""

        try:
            while True:
                chunk_bytes = conn.recv(4096)
                if not chunk_bytes:
                    break
                buffer += utf8_decoder.decode(chunk_bytes, final=False)

                # FIX #4: защита от переполнения буфера (медленный клиент / атака)
                if len(buffer) > _TCP_BUFFER_MAX:
                    print(f"[Server] Клиент uid={uid}: буфер превысил {_TCP_BUFFER_MAX}, отключаем")
                    break

                while True:
                    try:
                        msg, idx = _decoder.raw_decode(buffer)
                        buffer   = buffer[idx:].lstrip()
                        action   = msg.get('action')

                        # ── Login ─────────────────────────────────────────────
                        if action == CMD_LOGIN:
                            client_nick   = msg.get('nick', 'User')[:16]
                            client_avatar = msg.get('avatar', '1.svg')
                            _gcn = self._general_channel_name
                            with self.clients_lock:
                                self.clients[conn] = {
                                    'nick':         client_nick,
                                    'room':         _gcn,
                                    'uid':          uid,
                                    'avatar':       client_avatar,
                                    'ip':           client_ip,
                                    'status_icon':  '',
                                    'status_text':  '',
                                }
                            with self.udp_lock:
                                self.uid_to_room[uid] = _gcn
                            with self._host_order_lock:
                                if uid not in self._host_order:
                                    if self._is_embedded and client_ip == self._owner_ip:
                                        self._host_order.insert(0, uid)
                                        print(f"[Server] host_order: владелец {client_nick} "
                                              f"(uid={uid}) → position 0 (приоритет по IP)")
                                    else:
                                        self._host_order.append(uid)
                            self._safe_send(
                                conn,
                                json.dumps({'action': 'login_success', 'uid': uid}).encode('utf-8')
                            )
                            with self.clients_lock:
                                remaining = len(self.clients)
                            print(
                                f"[Server] ✔ {client_nick} подключился "
                                f"({_gcn}, IP: {client_ip}) | Онлайн: {remaining}"
                            )
                            self._mark_dirty()
                            self.send_global_state()

                        # ── Join Room ─────────────────────────────────────────
                        elif action == CMD_JOIN_ROOM:
                            _gcn_jr = self._general_channel_name
                            new_room = msg.get('room', _gcn_jr)[:CHANNEL_NAME_MAX_LEN]

                            with self._channels_lock:
                                ch = self._channels.get(new_room)

                            if new_room != _gcn_jr and ch is None:
                                self._safe_send(conn, json.dumps({
                                    'action': 'join_room_denied',
                                    'reason': 'not_found',
                                    'room':   new_room,
                                }).encode('utf-8'))
                            elif ch and ch['password'] is not None and not self._check_channel_auth(conn, new_room):
                                self._safe_send(conn, json.dumps({
                                    'action': 'join_room_denied',
                                    'reason': 'channel_auth_required',
                                    'room':   new_room,
                                }).encode('utf-8'))
                            else:
                                old_room = None
                                with self.clients_lock:
                                    if conn in self.clients:
                                        old_room = self.clients[conn].get('room')
                                        self.clients[conn]['room'] = new_room
                                with self.udp_lock:
                                    self.uid_to_room[uid] = new_room
                                if old_room and old_room != new_room:
                                    self._cleanup_temp_channels(old_room)
                                self._mark_dirty()
                                self.send_global_state()

                        # ── Аутентификация для защищённого канала ─────────────
                        elif action == CMD_JOIN_CHANNEL_AUTH:
                            ch_name = msg.get('channel_name', '')[:CHANNEL_NAME_MAX_LEN]
                            ch_pass = msg.get('password', '')[:CHANNEL_PASS_MAX_LEN]

                            with self._channels_lock:
                                ch = self._channels.get(ch_name)

                            if ch is None:
                                self._safe_send(conn, json.dumps({
                                    'action': 'channel_auth_result',
                                    'ok': False, 'reason': 'not_found',
                                    'channel_name': ch_name,
                                }).encode('utf-8'))
                            elif ch['password'] is None or ch['password'] == ch_pass:
                                with self._channel_auth_lock:
                                    self._channel_auth.setdefault(conn, set()).add(ch_name)
                                self._safe_send(conn, json.dumps({
                                    'action': 'channel_auth_result',
                                    'ok': True,
                                    'channel_name': ch_name,
                                }).encode('utf-8'))
                            else:
                                self._safe_send(conn, json.dumps({
                                    'action': 'channel_auth_result',
                                    'ok': False, 'reason': 'wrong_password',
                                    'channel_name': ch_name,
                                }).encode('utf-8'))

                        # ── Создание временного канала ──────────
                        elif action == CMD_CREATE_CHANNEL:
                            with self._host_order_lock:
                                is_host = bool(self._host_order and self._host_order[0] == uid)
                            if not is_host:
                                self._safe_send(conn, json.dumps({
                                    'action': 'create_channel_result',
                                    'ok': False, 'reason': 'not_host',
                                }).encode('utf-8'))
                            else:
                                ch_name = msg.get('channel_name', '').strip()[:CHANNEL_NAME_MAX_LEN]
                                ch_pass = msg.get('password', '').strip()[:CHANNEL_PASS_MAX_LEN] or None
                                if not ch_name or ch_name.lower() == 'general':
                                    self._safe_send(conn, json.dumps({
                                        'action': 'create_channel_result',
                                        'ok': False, 'reason': 'invalid_name',
                                    }).encode('utf-8'))
                                elif self._create_temp_channel(ch_name, ch_pass):
                                    self._safe_send(conn, json.dumps({
                                        'action': 'create_channel_result',
                                        'ok': True, 'channel_name': ch_name,
                                    }).encode('utf-8'))
                                    self._mark_dirty()
                                    self.send_global_state()
                                else:
                                    self._safe_send(conn, json.dumps({
                                        'action': 'create_channel_result',
                                        'ok': False, 'reason': 'already_exists',
                                    }).encode('utf-8'))

                        # ── Переименование постоянного канала ──
                        elif action == 'rename_channel':
                            with self._host_order_lock:
                                is_host = bool(self._host_order and self._host_order[0] == uid)
                            if is_host:
                                old_nm = msg.get('old_name', '').strip()[:CHANNEL_NAME_MAX_LEN]
                                new_nm = msg.get('new_name', '').strip()[:CHANNEL_NAME_MAX_LEN]
                                if old_nm and new_nm and old_nm != new_nm:
                                    with self._channels_lock:
                                        ch_data = self._channels.pop(old_nm, None)
                                        if ch_data is not None:
                                            self._channels[new_nm] = ch_data
                                            with self.clients_lock:
                                                for cdata in self.clients.values():
                                                    if cdata.get('room') == old_nm:
                                                        cdata['room'] = new_nm
                                            with self.udp_lock:
                                                for k, v in self.uid_to_room.items():
                                                    if v == old_nm:
                                                        self.uid_to_room[k] = new_nm
                                            self._general_channel_name = new_nm
                                            # FIX #6: переносим записи в _channel_auth
                                            with self._channel_auth_lock:
                                                for auth_set in self._channel_auth.values():
                                                    if old_nm in auth_set:
                                                        auth_set.discard(old_nm)
                                                        auth_set.add(new_nm)
                                            print(f"[Server] Канал переименован: '{old_nm}' → '{new_nm}'")
                                    self._mark_dirty()
                                    self.send_global_state()

                        # ── Update User ───────────────────────────────────────
                        elif action == 'update_user':
                            with self.clients_lock:
                                if conn in self.clients:
                                    self.clients[conn]['nick']   = msg.get('nick',   self.clients[conn]['nick'])
                                    self.clients[conn]['avatar'] = msg.get('avatar', self.clients[conn]['avatar'])
                            self._mark_dirty()
                            self.send_global_state()

                        # ── Update Status ─────────────────────────────────────
                        elif action == 'update_status':
                            new_mute = msg.get('mute', False)
                            new_deaf = msg.get('deaf', False)
                            _status_changed = False
                            with self.clients_lock:
                                if conn in self.clients:
                                    _status_changed = (
                                        self.clients[conn].get('mute') != new_mute
                                        or self.clients[conn].get('deaf') != new_deaf
                                    )
                                    self.clients[conn]['mute'] = new_mute
                                    self.clients[conn]['deaf'] = new_deaf
                            if _status_changed:
                                self._mark_dirty()
                                self.send_global_state()

                        # ── Ping Report ────────
                        elif action == 'report_ping':
                            ping_ms = msg.get('ping_ms', 0)
                            # FIX #9: защита от nan/inf (int(nan) → ValueError)
                            if (isinstance(ping_ms, (int, float))
                                    and math.isfinite(ping_ms)
                                    and ping_ms >= 0 and ping_ms < 60000):
                                with self._client_pings_lock:
                                    self._client_pings[uid] = int(ping_ms)

                        # ── Presence ──────────────────────────────────────────
                        elif action == CMD_UPDATE_PRESENCE:
                            icon = msg.get('status_icon', '')[:64]
                            text = msg.get('status_text', '')[:30]
                            with self.clients_lock:
                                if conn in self.clients:
                                    self.clients[conn]['status_icon'] = icon
                                    self.clients[conn]['status_text'] = text
                            self._mark_dirty()
                            self.send_global_state()

                        # ── Stream Start ──────────────────────────────────────
                        elif action == CMD_STREAM_START:
                            started_uid = None
                            with self.clients_lock:
                                if conn in self.clients:
                                    self.clients[conn]['is_streaming'] = True
                                    self.clients[conn]['sfu_port'] = msg.get('sfu_port', 7788)
                                    started_uid = self.clients[conn]['uid']
                                    print(f"[Server] {self.clients[conn]['nick']} запустил стрим (SFU порт={self.clients[conn]['sfu_port']})")

                            if started_uid is not None:
                                with self.watchers_lock:
                                    existing_watchers = dict(self.watchers.get(started_uid, {}))

                                if existing_watchers:
                                    notify = json.dumps({
                                        'action': 'streamer_reconnected',
                                        'streamer_uid': started_uid,
                                    }).encode('utf-8')
                                    notified = 0
                                    with self.clients_lock:
                                        _watchers_to_notify = [
                                            c for c, info in self.clients.items()
                                            if info.get('uid') in existing_watchers
                                        ]
                                    # SENIOR FIX: вышли из-под clients_lock перед I/O
                                    # (sendall может блокироваться на медленном клиенте)
                                    for c in _watchers_to_notify:
                                        self._safe_send(c, notify)
                                        notified += 1
                                    print(f"[Server] streamer_reconnected uid={started_uid} → {notified} зрителей")

                            self._mark_dirty()
                            self.send_global_state()

                        # ── Stream Stop ───────────────────────────────────────
                        elif action == CMD_STREAM_STOP:
                            stopped_uid = None
                            with self.clients_lock:
                                if conn in self.clients:
                                    self.clients[conn]['is_streaming'] = False
                                    stopped_uid = self.clients[conn]['uid']
                                    print(f"[Server] {self.clients[conn]['nick']} остановил стрим")
                            if stopped_uid is not None:
                                with self.watchers_lock:
                                    self.watchers.pop(stopped_uid, None)
                                _sfu = self.sfu
                                if _sfu:
                                    _sfu.close_streamer(stopped_uid)
                            self._mark_dirty()
                            self.send_global_state()

                        # ── Stream Watch Start ────────────────────────────────
                        elif action == 'stream_watch_start':
                            streamer_uid = msg.get('streamer_uid')
                            if streamer_uid is not None:
                                # FIX #1: все w_uid/watcher_nick/watcher_avatar
                                # защищены проверкой conn in clients.
                                w_uid = watcher_nick = watcher_avatar = None
                                with self.clients_lock:
                                    if conn in self.clients:
                                        watcher        = self.clients[conn]
                                        w_uid          = watcher['uid']
                                        watcher_nick   = watcher['nick']
                                        watcher_avatar = watcher.get('avatar', '1.svg')

                                if w_uid is not None:
                                    with self.watchers_lock:
                                        if streamer_uid not in self.watchers:
                                            self.watchers[streamer_uid] = {}
                                        self.watchers[streamer_uid][w_uid] = {
                                            'uid':    w_uid,
                                            'nick':   watcher_nick,
                                            'avatar': watcher_avatar,
                                        }
                                    print(
                                        f"[Server] {watcher_nick} "
                                        f"начал смотреть стрим uid={streamer_uid}"
                                    )
                                    _sfu = self.sfu
                                    if _sfu:
                                        quality = msg.get('quality', 'hq')
                                        streamer_ip = '127.0.0.1'
                                        streamer_sfu_port = 7788
                                        with self.clients_lock:
                                            for _c in self.clients.values():
                                                if _c.get('uid') == streamer_uid:
                                                    raw_ip = _c.get('ip', '127.0.0.1')
                                                    if self._is_embedded and raw_ip == self._owner_ip:
                                                        streamer_ip = '127.0.0.1'
                                                    else:
                                                        streamer_ip = raw_ip
                                                    streamer_sfu_port = _c.get('sfu_port', 7788)
                                                    break
                                        _sfu.trigger_viewer_connect(
                                            w_uid, streamer_uid, conn, quality,
                                            streamer_ip=streamer_ip,
                                            streamer_sfu_port=streamer_sfu_port,
                                        )
                            self._mark_dirty()
                            self.send_global_state()

                        # ── Stream Watch Stop ─────────────────────────────────
                        elif action == 'stream_watch_stop':
                            streamer_uid = msg.get('streamer_uid')
                            if streamer_uid is not None:
                                with self.clients_lock:
                                    w_uid = self.clients[conn]['uid'] if conn in self.clients else None
                                if w_uid is not None:
                                    with self.watchers_lock:
                                        if streamer_uid in self.watchers:
                                            self.watchers[streamer_uid].pop(w_uid, None)
                                    with self.clients_lock:
                                        nick = self.clients[conn]['nick'] if conn in self.clients else '?'
                                    print(f"[Server] {nick} перестал смотреть стрим uid={streamer_uid}")
                                    _sfu = self.sfu
                                    if _sfu:
                                        _sfu.close_viewer(w_uid)
                            self._mark_dirty()
                            self.send_global_state()

                        # ── WebRTC Offer ──────────────────────────────────────
                        elif action == CMD_WEBRTC_OFFER:
                            sdp      = msg.get('sdp')
                            sdp_type = msg.get('type', 'offer')
                            role     = msg.get('role', '')

                            if not self.sfu:
                                continue

                            print(f"[Server] CMD_WEBRTC_OFFER: role={role!r}, uid={uid}, sdp_len={len(sdp) if sdp else 0}")
                            if role == 'viewer_offer':
                                if sdp:
                                    self.sfu.handle_viewer_offer(uid, sdp, conn)
                                else:
                                    print(f"[Server] ❌ viewer_offer с пустым SDP от uid={uid}")
                            elif role == 'streamer':
                                print(f"[Server] CMD_WEBRTC_OFFER role=streamer uid={uid}: ignored in v3")
                            elif role == 'viewer':
                                print(f"[Server] CMD_WEBRTC_OFFER role=viewer uid={uid}: триггер старого клиента")
                            else:
                                print(f"[Server] CMD_WEBRTC_OFFER неизвестный role={role!r} uid={uid}")

                        elif action == CMD_WEBRTC_ANSWER:
                            pass  # v3: answer идёт server→viewer

                        elif action == CMD_WEBRTC_ICE:
                            pass  # v3: gather-complete ICE

                        # ── Передача сервера (2-шаговая) ────
                        elif action == CMD_SERVER_TRANSFER:
                            target_uid_st = msg.get('target_uid')
                            if not isinstance(target_uid_st, int):
                                continue
                            with self.clients_lock:
                                snap = dict(self.clients)
                            self._handle_server_transfer_v2(
                                uid, target_uid_st, snap, conn
                            )

                        # ── Целевой клиент подтверждает готовность ─────────────
                        elif action == CMD_MIGRATE_READY:
                            self._handle_migrate_ready(uid)

                        # ── Soundboard ────────────────────────────────────────
                        elif action == CMD_SOUNDBOARD:
                            with self.clients_lock:
                                sender_nick = self.clients[conn]['nick'] if conn in self.clients else '?'
                                conns = list(self.clients.keys())
                            msg['from_nick'] = sender_nick
                            payload = json.dumps(msg).encode('utf-8')
                            # SENIOR FIX: _safe_send вместо прямого sendall
                            for c in conns:
                                self._safe_send(c, payload)

                        # ── Nudge Vote ────────────────────────────────────────
                        elif action == CMD_NUDGE_VOTE:
                            self._process_nudge_vote(conn, uid, msg)

                        # ── Файловая передача P2P ─────────────────────────────
                        elif action == CMD_FILE_OFFER:
                            target_uid_fo = msg.get('target_uid')
                            if isinstance(target_uid_fo, int):
                                msg['sender_ip'] = client_ip
                                payload_fo = json.dumps(msg).encode('utf-8')
                                target_conn_fo = None
                                with self.clients_lock:
                                    for c_conn, c_data in self.clients.items():
                                        if c_data['uid'] == target_uid_fo:
                                            target_conn_fo = c_conn
                                            break
                                if target_conn_fo:
                                    # SENIOR FIX: _safe_send — гонка с broadcaster-потоком
                                    self._safe_send(target_conn_fo, payload_fo)
                                    print(f"[Server] 📁 file_offer: uid={uid} → uid={target_uid_fo}")

                        elif action == CMD_FILE_OFFER_ROOM:
                            msg['sender_ip'] = client_ip
                            payload_for = json.dumps(msg).encode('utf-8')
                            room_conns = []
                            with self.clients_lock:
                                sender_room_fo = (
                                    self.clients[conn]['room'] if conn in self.clients else None
                                )
                                if sender_room_fo:
                                    room_conns = [
                                        c_conn
                                        for c_conn, c_data in self.clients.items()
                                        if c_data['room'] == sender_room_fo
                                        and c_data['uid'] != uid
                                    ]
                            # SENIOR FIX: _safe_send вместо прямого sendall
                            for rc in room_conns:
                                self._safe_send(rc, payload_for)
                            if room_conns:
                                print(f"[Server] 📁 file_offer_room: uid={uid} → {len(room_conns)} получателей")

                        # ── Быстрый чат ───────────────────────────────────────
                        elif action == CMD_QUICK_MSG:
                            text = str(msg.get('text', '')).strip()[:QUICK_MSG_MAX_LEN]
                            if text:
                                self._process_quick_msg(conn, uid, text)

                        # ── Постоянный чат: новое сообщение ───────────────────
                        elif action == CMD_CHAT_MSG:
                            text_cm = str(msg.get('text', '')).strip()[:CHAT_MSG_MAX_LEN]
                            if text_cm:
                                self._process_chat_msg(conn, uid, text_cm)

                        elif action == CMD_CHAT_HISTORY_REQ:
                            self._process_chat_history_req(conn)

                        elif action == CMD_CHAT_HISTORY:
                            target_uid_ch = int(msg.get('target_uid', 0))
                            messages_ch   = msg.get('messages', [])
                            if target_uid_ch and isinstance(messages_ch, list):
                                with self.clients_lock:
                                    target_conn_ch = next(
                                        (c for c, d in self.clients.items()
                                         if d.get('uid') == target_uid_ch),
                                        None
                                    )
                                if target_conn_ch:
                                    # SENIOR FIX: _safe_send вместо прямого sendall
                                    self._safe_send(target_conn_ch, json.dumps({
                                        'action':   CMD_CHAT_HISTORY,
                                        'messages': messages_ch,
                                    }).encode('utf-8'))

                        elif action == CMD_TYPING:
                            self._process_typing(conn)

                        elif action == CMD_CHAT_MEDIA:
                            file_data_b64 = msg.get('file_data_b64', '')
                            if (file_data_b64
                                    and len(file_data_b64) <= CHAT_MEDIA_MAX_B64):
                                self._process_chat_media(conn, msg, file_data_b64)

                        # ── Хост выключает микрофон участника ─────────────────
                        elif action == CMD_HOST_MUTE:
                            with self._host_order_lock:
                                is_host = bool(
                                    self._host_order and self._host_order[0] == uid
                                )
                            if is_host:
                                target_uid_hm = int(msg.get('target_uid', 0))
                                if target_uid_hm and target_uid_hm != uid:
                                    with self.clients_lock:
                                        target_conn_hm = next(
                                            (c for c, d in self.clients.items()
                                             if d.get('uid') == target_uid_hm),
                                            None
                                        )
                                    if target_conn_hm:
                                        # SENIOR FIX: _safe_send
                                        self._safe_send(target_conn_hm, json.dumps({
                                            'action': CMD_FORCE_MUTED,
                                        }).encode('utf-8'))

                        elif action == CMD_DRAW_STROKE:
                            self._process_draw_stroke(conn, msg)

                    except json.JSONDecodeError:
                        break

        except Exception as e:
            err_code = getattr(e, 'winerror', None) or getattr(e, 'errno', None)
            is_disconnect = err_code in (10054, 10053, 104, 32)
            if not is_disconnect:
                print(f"[Server] TCP ошибка: {e}")

        finally:
            # Очистка при отключении
            u_id = None
            nick = 'Unknown'
            room = '?'

            with self.clients_lock:
                if conn in self.clients:
                    client_info = self.clients.pop(conn)
                    nick      = client_info.get('nick', 'Unknown')
                    room      = client_info.get('room', '?')
                    u_id      = client_info['uid']
                    remaining = len(self.clients)
                else:
                    remaining = len(self.clients)

            if u_id is not None:
                with self.udp_lock:
                    self.udp_map.pop(u_id, None)
                    self.uid_to_room.pop(u_id, None)

                with self._host_order_lock:
                    if u_id in self._host_order:
                        self._host_order.remove(u_id)

                with self._channel_auth_lock:
                    self._channel_auth.pop(conn, None)

                with self._client_pings_lock:
                    self._client_pings.pop(u_id, None)

                with self.nudge_lock:
                    for r_votes in self.nudge_votes.values():
                        r_votes.pop(u_id, None)
                        for target_votes in r_votes.values():
                            target_votes.pop(u_id, None)

                # FIX #7: сравнение с _general_channel_name вместо хардкод 'General'
                if room and room != self._general_channel_name:
                    self._cleanup_temp_channels(room)

                with self.watchers_lock:
                    for s_uid in list(self.watchers.keys()):
                        if u_id in self.watchers[s_uid]:
                            self.watchers[s_uid].pop(u_id, None)
                    self.watchers.pop(u_id, None)

                _sfu = self.sfu
                if _sfu:
                    _sfu.close_streamer(u_id)
                    _sfu.close_viewer(u_id)

                print(
                    f"[Server] ✖ {nick} (комната: {room}) "
                    f"отключился | Онлайн: {remaining}"
                )
            else:
                print(f"[Server] ✖ Незарегистрированный клиент {addr[0]} отключился")

            try:
                conn.close()
            except Exception:
                pass
            # FIX #47: очищаем per-conn lock чтобы не накапливались dict ключей
            try:
                from .server_webrtc import _drop_conn_lock
                _drop_conn_lock(conn)
            except Exception:
                pass
            self._mark_dirty()
            self.send_global_state()

    # ------------------------------------------------------------------
    # Вспомогательные методы — вынесены из tcp_handler для читаемости
    # ------------------------------------------------------------------

    def _process_nudge_vote(self, conn, uid: int, msg: dict) -> None:
        """Обработка голосования «Пнуть»."""
        target_uid = msg.get('target_uid')
        if not isinstance(target_uid, int):
            return

        now  = time.time()
        fire = False
        t_conn            = None
        broadcaster_conns = []
        voter_nick   = '?'
        target_nick  = '?'
        voter_uid_v  = None
        voter_room   = None

        with self.clients_lock:
            if conn not in self.clients:
                return
            voter_info  = self.clients[conn]
            voter_uid_v = voter_info['uid']
            voter_room  = voter_info['room']
            voter_nick  = voter_info['nick']

            room_uids = [
                c['uid'] for c in self.clients.values()
                if c['room'] == voter_room
            ]
            voters_count = max(1, len(room_uids) - 1)
            threshold = max(1, (voters_count + 1) // 2)

            for c_conn, c_data in self.clients.items():
                if (c_data['uid'] == target_uid
                        and c_data['room'] == voter_room):
                    t_conn      = c_conn
                    target_nick = c_data['nick']
                    break

            broadcaster_conns = [
                c_conn for c_conn, c_data in self.clients.items()
                if c_data['room'] == voter_room
            ]

        if t_conn is None:
            return

        with self.nudge_lock:
            room_votes   = self.nudge_votes.setdefault(voter_room, {})
            target_votes = room_votes.setdefault(target_uid, {})

            last = target_votes.get(voter_uid_v, 0)
            if now - last < NUDGE_COOLDOWN_SEC:
                remaining = int(NUDGE_COOLDOWN_SEC - (now - last))
                print(
                    f"[Server] 👟 {voter_nick} → Пнуть {target_nick}"
                    f" — кулдаун ещё {remaining} с"
                )
                return

            target_votes[voter_uid_v] = now

            active = sum(
                1 for _, ts in target_votes.items()
                if now - ts < NUDGE_COOLDOWN_SEC
            )
            print(
                f"[Server] 👟 {voter_nick} → Пнуть {target_nick}"
                f" ({active}/{threshold} голосов)"
            )

            if active >= threshold:
                room_votes.pop(target_uid, None)
                fire = True

        if fire:
            # SENIOR FIX: _safe_send вместо прямого sendall
            self._safe_send(
                t_conn,
                json.dumps({'action': CMD_PLAY_NUDGE}).encode('utf-8')
            )
            print(f"[Server] 👟 NUDGE FIRED → {target_nick}")

            broadcast_payload = json.dumps({
                'action':      CMD_NUDGE_TRIGGERED,
                'target_nick': target_nick,
                'voter_nick':  voter_nick,
            }).encode('utf-8')
            for bc in broadcaster_conns:
                self._safe_send(bc, broadcast_payload)

    def _process_quick_msg(self, conn, uid: int, text: str) -> None:
        with self.clients_lock:
            sender_nick = (
                self.clients[conn]['nick']
                if conn in self.clients else '?'
            )
            sender_uid_qm = (
                self.clients[conn]['uid']
                if conn in self.clients else 0
            )
            sender_room_qm = (
                self.clients[conn]['room']
                if conn in self.clients else None
            )
            room_conns_qm = []
            if sender_room_qm:
                room_conns_qm = [
                    c_conn
                    for c_conn, c_data in self.clients.items()
                    if c_data['room'] == sender_room_qm
                ]
        if sender_uid_qm:
            broadcast_qm = json.dumps({
                'action':     CMD_QUICK_MSG,
                'uid':        sender_uid_qm,
                'from_nick':  sender_nick,
                'text':       text,
            }).encode('utf-8')
            # SENIOR FIX: _safe_send
            for bc in room_conns_qm:
                self._safe_send(bc, broadcast_qm)

    def _process_chat_msg(self, conn, uid: int, text_cm: str) -> None:
        sender_nick_cm = sender_uid_cm = sender_room_cm = None
        sender_avatar_cm = ''
        room_conns_cm = []
        with self.clients_lock:
            if conn in self.clients:
                c = self.clients[conn]
                sender_nick_cm   = c['nick']
                sender_uid_cm    = c['uid']
                sender_avatar_cm = c.get('avatar', '')
                sender_room_cm   = c.get('room')
            if sender_room_cm:
                room_conns_cm = [
                    c_conn
                    for c_conn, c_data in self.clients.items()
                    if c_data.get('room') == sender_room_cm
                ]
        if sender_uid_cm:
            ts_cm = time.time()
            broadcast_cm = json.dumps({
                'action':    CMD_CHAT_MSG,
                'uid':       sender_uid_cm,
                'from_nick': sender_nick_cm,
                'avatar':    sender_avatar_cm,
                'text':      text_cm,
                'ts':        ts_cm,
                'room':      sender_room_cm or '',
            }).encode('utf-8')
            if self._chat_db:
                self._chat_db.add_message({
                    'uid': sender_uid_cm, 'nick': sender_nick_cm,
                    'avatar': sender_avatar_cm, 'text': text_cm,
                    'room': sender_room_cm or '', 'ts': ts_cm,
                })
            # SENIOR FIX: _safe_send
            for bc in room_conns_cm:
                self._safe_send(bc, broadcast_cm)

    def _process_chat_history_req(self, conn) -> None:
        requester_room = None
        with self.clients_lock:
            if conn in self.clients:
                requester_room = self.clients[conn].get('room', '')
        if requester_room:
            messages_db = (
                self._chat_db.get_history(requester_room, CHAT_HISTORY_MAX)
                if self._chat_db else []
            )
            if messages_db:
                # SENIOR FIX: _safe_send
                self._safe_send(conn, json.dumps({
                    'action':   CMD_CHAT_HISTORY,
                    'messages': messages_db,
                }).encode('utf-8'))

    def _process_typing(self, conn) -> None:
        t_conns: list = []
        t_payload: bytes | None = None
        with self.clients_lock:
            if conn in self.clients:
                c_t = self.clients[conn]
                t_uid  = c_t['uid']
                t_nick = c_t['nick']
                t_room = c_t.get('room')
                if t_room:
                    t_payload = json.dumps({
                        'action': CMD_TYPING,
                        'uid':    t_uid,
                        'nick':   t_nick,
                    }).encode('utf-8')
                    t_conns = [
                        cc for cc, cd in self.clients.items()
                        if cd.get('room') == t_room and cc is not conn
                    ]
        if t_payload is not None:
            # SENIOR FIX: _safe_send
            for tc in t_conns:
                self._safe_send(tc, t_payload)

    def _process_chat_media(self, conn, msg: dict, file_data_b64: str) -> None:
        """Медиа-вложение в постоянный чат. Использует MD5-кэш для сериализации."""
        s_nick_md = s_uid_md = s_room_md = None
        s_avatar_md = ''
        room_conns_md: list = []
        with self.clients_lock:
            if conn in self.clients:
                c = self.clients[conn]
                s_nick_md   = c['nick']
                s_uid_md    = c['uid']
                s_avatar_md = c.get('avatar', '')
                s_room_md   = c.get('room')
            if s_room_md:
                room_conns_md = [
                    c_conn
                    for c_conn, c_data in self.clients.items()
                    if c_data.get('room') == s_room_md
                ]
        if not s_uid_md:
            return

        now_ts = time.time()
        md5_key = hashlib.md5(
            file_data_b64.encode('utf-8'), usedforsecurity=False
        ).hexdigest()

        broadcast_md: bytes | None = None

        with self._media_cache_lock:
            cached = self._media_cache.get(md5_key)
            # FIX #3: тупл 5-элементный (ts, prefix, suffix, uid, nick)
            hit = (cached is not None
                   and cached[0] > now_ts
                   and cached[3] == s_uid_md
                   and cached[4] == s_nick_md)

        # SENIOR FIX: раньше кэш пытался разрезать готовый JSON по байтам
        # "ts": <number>, что зависело от того, что Python `f"{float}"`
        # и `json.dumps(float)` дадут одинаковую репрезентацию. Для
        # большинства float это правда, но для пограничных значений
        # (NaN, субнормальные, миллисекундные тики с точным float64-repr)
        # порядок записи float мог расходиться → cache miss после первого
        # удачного кэширования или KeyError на index().
        #
        # Новый подход: кэшируем JSON-объект БЕЗ поля 'ts' (всегда новое),
        # храним как bytes без закрывающей '}'. При отправке добавляем
        # ',"ts":<now_ts>}' — это всегда валидный JSON.
        if hit:
            cached_prefix: bytes = cached[1]  # JSON без '}' и без ts
            broadcast_md = cached_prefix + f',"ts":{now_ts}}}'.encode('ascii')
        else:
            full_dict = {
                'action':        CMD_CHAT_MEDIA,
                'uid':           s_uid_md,
                'from_nick':     s_nick_md,
                'avatar':        s_avatar_md,
                'room':          s_room_md or '',
                'file_name':     msg.get('file_name', 'file'),
                'file_type':     msg.get('file_type', ''),
                'file_data_b64': file_data_b64,
            }
            # Кэшируем JSON без '}' в конце — потом дописываем ",ts":... и "}"
            prefix_bytes = json.dumps(full_dict, ensure_ascii=False).encode('utf-8')
            assert prefix_bytes.endswith(b'}'), "json.dumps должен заканчиваться на }"
            cached_prefix = prefix_bytes[:-1]  # без закрывающей скобки

            # Итоговый broadcast — сразу с ts
            broadcast_md = cached_prefix + f',"ts":{now_ts}}}'.encode('ascii')

            # Сохраняем в кэш (с эвикшеном по TTL/size)
            try:
                with self._media_cache_lock:
                    if len(self._media_cache) >= self._media_cache_max:
                        expired = [k for k, v in self._media_cache.items()
                                   if v[0] <= now_ts]
                        for k in expired:
                            del self._media_cache[k]
                        if len(self._media_cache) >= self._media_cache_max:
                            oldest = min(self._media_cache,
                                         key=lambda k: self._media_cache[k][0])
                            del self._media_cache[oldest]
                    # 5-tuple совместимый с существующей аннотацией:
                    # (expire_ts, prefix_bytes, empty_suffix, uid, nick)
                    self._media_cache[md5_key] = (
                        now_ts + self._media_cache_ttl,
                        cached_prefix, b'',  # suffix пустой — legacy slot
                        s_uid_md, s_nick_md,
                    )
            except Exception:
                pass  # кэш не удался — broadcast_md уже готов

        # SENIOR FIX: _safe_send
        for bc in room_conns_md:
            self._safe_send(bc, broadcast_md)

        if self._chat_db:
            self._chat_db.add_message({
                'uid': s_uid_md, 'nick': s_nick_md,
                'avatar': s_avatar_md,
                'room': s_room_md or '', 'ts': now_ts,
                'file_name': msg.get('file_name', 'file'),
                'file_type': msg.get('file_type', ''),
                'file_data_b64': file_data_b64,
            })

    def _process_draw_stroke(self, conn, msg: dict) -> None:
        streamer_uid_dr = msg.get('streamer_uid')
        points_dr       = msg.get('points', [])
        color_dr        = str(msg.get('color', '#FF6B6B'))[:16]
        width_dr        = max(1, min(8, int(msg.get('width', 3))))
        nick_dr         = str(msg.get('nick', '?'))[:32]

        if not (streamer_uid_dr and isinstance(points_dr, list) and points_dr):
            return

        if len(points_dr) > DRAW_MAX_POINTS:
            points_dr = points_dr[:DRAW_MAX_POINTS]

        with self.clients_lock:
            sender_uid_dr = (
                self.clients[conn]['uid']
                if conn in self.clients else 0
            )

        relay_dr = json.dumps({
            'action':       CMD_DRAW_STROKE,
            'streamer_uid': streamer_uid_dr,
            'sender_uid':   sender_uid_dr,
            'nick':         nick_dr,
            'color':        color_dr,
            'points':       points_dr,
            'width':        width_dr,
        }).encode('utf-8')

        with self.watchers_lock:
            watcher_uids_dr = set(
                self.watchers.get(streamer_uid_dr, {}).keys()
            )

        with self.clients_lock:
            target_conns_dr = [
                c for c, d in self.clients.items()
                if d.get('uid') in watcher_uids_dr
            ]
            streamer_conn_dr = next(
                (c for c, d in self.clients.items()
                 if d.get('uid') == streamer_uid_dr),
                None,
            )
            if streamer_conn_dr:
                target_conns_dr.append(streamer_conn_dr)

        # SENIOR FIX: _safe_send
        for tc in target_conns_dr:
            self._safe_send(tc, relay_dr)

    # ------------------------------------------------------------------
    # Отправка пакета зрителям стримера (UDP)
    # ------------------------------------------------------------------
    def _send_to_watchers(self, sender_uid: int, data: bytes):
        with self.watchers_lock:
            watcher_uids = list(self.watchers.get(sender_uid, {}).keys())

        with self.udp_lock:
            target_addrs = [
                self.udp_map[uid]
                for uid in watcher_uids
                if uid in self.udp_map
            ]

        for addr in target_addrs:
            try:
                self.udp_sock.sendto(data, addr)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Рассылка глобального состояния (FIX #5)
    # ------------------------------------------------------------------
    def _mark_dirty(self) -> None:
        """Помечает кэш payload как устаревший."""
        # FIX #5: эта операция простая bool-запись, атомарна через GIL.
        # Проблема была в том, что ОЧИСТКА флага после сборки payload
        # не была атомарной относительно mark_dirty. Теперь сборка + сброс
        # защищены clients_lock (см. send_global_state).
        self._state_dirty = True

    def send_global_state(self):
        """
        Кэшированная версия — payload пересобирается только при изменениях.

        FIX #5: check-and-clear защищён clients_lock. Раньше было race:
        1. send_global_state прочитал dirty=True
        2. начал сборку
        3. ДРУГОЙ поток изменил state + вызвал _mark_dirty() (dirty=True)
        4. send_global_state закончил сборку и поставил dirty=False
        → изменение из шага 3 потеряно, новый payload содержит старое состояние.

        Решение: захват dirty и очистка выполняются под clients_lock
        одновременно со сбором snapshot'а clients.
        """
        payload = None

        with self.clients_lock:
            dirty_now = self._state_dirty
            if dirty_now:
                self._state_dirty = False  # сбрасываем ПЕРЕД сборкой
                # Собираем snapshot всего нужного под одним локом
                clients_snap = {
                    c_conn: dict(c_data)
                    for c_conn, c_data in self.clients.items()
                }
                conns_snapshot = list(self.clients.keys())
            else:
                conns_snapshot = list(self.clients.keys())
                clients_snap = None

        if dirty_now and clients_snap is not None:
            with self.watchers_lock:
                watchers_snapshot = {uid: dict(ws) for uid, ws in self.watchers.items()}

            with self._host_order_lock:
                host_order_snapshot = list(self._host_order)
                server_host_uid = self._host_order[0] if self._host_order else 0

            state = {}
            for c_conn, c in clients_snap.items():
                c_uid = c['uid']
                watchers_list = list(watchers_snapshot.get(c_uid, {}).values())
                state.setdefault(c['room'], []).append({
                    'nick':         c['nick'],
                    'uid':          c_uid,
                    'avatar':       c.get('avatar', '1.svg'),
                    'mute':         c.get('mute', False),
                    'deaf':         c.get('deaf', False),
                    'ip':           c.get('ip', ''),
                    'is_streaming': c.get('is_streaming', False),
                    'sfu_port':     c.get('sfu_port', 7788),
                    'watchers':     watchers_list,
                    'status_icon':  c.get('status_icon', ''),
                    'status_text':  c.get('status_text', ''),
                })

            self._cached_payload = json.dumps(
                {'action': CMD_SYNC_USERS, 'all_users': state,
                 'host_order': host_order_snapshot,
                 'server_host_uid': server_host_uid,
                 'channel_list': self._get_channel_list()}
            ).encode('utf-8')

        payload = self._cached_payload
        if payload is None:
            return

        # FIX: кладём в broadcaster-очередь вместо синхронного sendall.
        # Если очередь переполнена (64 слота) — drop: клиенты обновятся
        # при следующем изменении состояния.
        try:
            self._bcast_queue.put_nowait((payload, conns_snapshot))
        except _queue.Full:
            pass

    # ------------------------------------------------------------------
    # Встроенный сервер: запуск
    # ------------------------------------------------------------------
    def start_embedded(self, host_ip: str, host_nick: str) -> None:
        """Запускает сервер в фоновых потоках."""
        self._is_embedded = True
        self._accepting   = True

        if self._chat_db is None:
            try:
                self._chat_db = ChatDB()
                print("[Server] ChatDB инициализирован")
            except Exception as e:
                print(f"[Server] ChatDB init error: {e}")
        self._owner_ip = host_ip

        # FIX #10: более точная обработка ошибок SFU-инициализации.
        try:
            from network_engine.sfu_bridge import get_shared as _get_sfu
        except ImportError:
            print("[Server] SfuBridge модуль не найден — WebRTC недоступен")
        else:
            try:
                _sfu_bridge = _get_sfu(
                    on_log=lambda s: print(f"[SFU] {s}"),
                    on_exit=lambda c: print(f"[SFU] завершён (code={c})"),
                )
                if not _sfu_bridge.is_running():
                    _sfu_bridge.start()
                if _sfu_bridge.is_running():
                    self.sfu = PionSfuProxy(sfu_bridge=_sfu_bridge)
                    print("[Server] Pion SFU подключён (встроенный режим)")
                else:
                    print("[Server] Pion SFU не запустился — WebRTC недоступен")
            except Exception as e:
                print(f"[Server] SFU init error: {e}")

        threading.Thread(target=self.udp_handler,   daemon=True, name="srv-udp").start()
        threading.Thread(target=self.stats_monitor, daemon=True, name="srv-stats").start()
        threading.Thread(
            target=self._embedded_accept_loop,
            daemon=True,
            name="srv-accept",
        ).start()

        try:
            from network_engine.server_discovery import ServerAnnouncer
            self._announcer = ServerAnnouncer(
                server_ip      = host_ip,
                server_port    = DEFAULT_PORT_TCP,
                host_nick      = host_nick,
                server_name    = self._server_name,
                get_user_count = self.get_client_count,
                get_user_nicks = self._get_user_nicks_list,
            )
            self._announcer.start()
        except Exception as e:
            print(f"[Server] ServerAnnouncer error: {e}")

        print(
            f"[Server] Встроенный сервер запущен. "
            f"TCP:{DEFAULT_PORT_TCP}, UDP:{DEFAULT_PORT_UDP}, IP={host_ip}"
        )

    def _embedded_accept_loop(self) -> None:
        self.tcp_sock.settimeout(1.0)
        while self._accepting:
            try:
                conn, addr = self.tcp_sock.accept()
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                threading.Thread(
                    target=self.tcp_handler,
                    args=(conn, addr),
                    daemon=True,
                ).start()
            except socket.timeout:
                continue
            except OSError:
                break

    def stop_gracefully(self) -> None:
        """Корректная остановка с передачей хостинга."""
        if not self._is_embedded:
            return

        self._accepting = False

        if self._announcer:
            self._announcer.stop()
            self._announcer = None

        with self._host_order_lock:
            my_uid = self._host_order[0] if self._host_order else 0

        next_uid, next_ip = self._pick_best_host(exclude_uid=my_uid)

        if not next_uid:
            print("[Server] stop_gracefully: нет других участников, закрываемся")
            try:
                self.tcp_sock.close()
            except Exception:
                pass
            try:
                self.udp_sock.close()
            except Exception:
                pass
            return

        print(f"[Server] stop_gracefully: передаём хостинг uid={next_uid} ({next_ip})")
        self._broadcast_server_migrate(next_uid, next_ip)

        time.sleep(0.35)

        with self.clients_lock:
            conns = list(self.clients.keys())
        for c in conns:
            try:
                c.shutdown(socket.SHUT_WR)
            except Exception:
                pass

        try:
            self.tcp_sock.close()
        except Exception:
            pass
        try:
            self.udp_sock.close()
        except Exception:
            pass

        if self.sfu is not None:
            try:
                self.sfu.shutdown()
            except Exception as e:
                print(f"[Server] SFU shutdown error: {e}")
            self.sfu = None

        print("[Server] Встроенный сервер остановлен")

    def stop_silent(self) -> None:
        """Немедленная тихая остановка без broadcast."""
        if not self._is_embedded:
            return
        print("[Server] stop_silent: освобождаем сокеты")
        self._accepting = False

        if self._announcer:
            try:
                self._announcer.stop()
            except Exception:
                pass
            self._announcer = None

        with self.clients_lock:
            conns = list(self.clients.keys())
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
                c.close()
            except Exception:
                pass

        try:
            self.tcp_sock.close()
        except Exception:
            pass
        try:
            self.udp_sock.close()
        except Exception:
            pass

        if self.sfu is not None:
            try:
                self.sfu.shutdown()
            except Exception as e:
                print(f"[Server] SFU shutdown error: {e}")
            self.sfu = None

        print("[Server] stop_silent: готово")

    # ------------------------------------------------------------------
    # 2-шаговая ручная передача
    # ------------------------------------------------------------------
    def _handle_server_transfer_v2(self, uid: int, target_uid: int,
                                    clients_snapshot: dict,
                                    initiator_conn) -> None:
        with self._host_order_lock:
            is_host = bool(self._host_order and self._host_order[0] == uid)
        if not is_host:
            print(f"[Server] server_transfer от uid={uid}: не хост — отклонено")
            return

        if target_uid == 0:
            target_uid, target_ip = self._pick_best_host(exclude_uid=uid)
            if not target_uid:
                print("[Server] server_transfer auto: нет кандидатов")
                return
        else:
            target_ip = None
            for c_data in clients_snapshot.values():
                if c_data['uid'] == target_uid:
                    target_ip = c_data.get('ip')
                    break
            if not target_ip:
                print(f"[Server] server_transfer: uid={target_uid} не найден")
                return

        target_conn = None
        for conn_key, c_data in clients_snapshot.items():
            if c_data['uid'] == target_uid:
                target_conn = conn_key
                break
        if target_conn is None:
            print(f"[Server] server_transfer: conn для uid={target_uid} не найден")
            return

        with self._pending_migrate_lock:
            if self._pending_migrate_target is not None:
                print(f"[Server] server_transfer: уже идёт передача к "
                      f"{self._pending_migrate_target} — отклонено")
                return
            self._pending_migrate_target = target_uid
            self._pending_migrate_ready  = threading.Event()

        target_nick = '?'
        for c_data in clients_snapshot.values():
            if c_data['uid'] == target_uid:
                target_nick = c_data.get('nick', '?')
                break

        print(f"[Server] 🔀 Передача сервера: {uid} → {target_nick} "
              f"(uid={target_uid}, IP={target_ip})")

        try:
            with self._host_order_lock:
                order_snap = list(self._host_order)
            # SENIOR FIX: _safe_send — per-conn lock защищает от гонки с broadcaster
            self._safe_send(target_conn, json.dumps({
                'action':     CMD_MIGRATE_PREPARE,
                'host_order': order_snap,
            }).encode('utf-8'))
        except Exception as e:
            print(f"[Server] migrate_prepare send failed: {e}")
            with self._pending_migrate_lock:
                self._pending_migrate_target = None
                self._pending_migrate_ready  = None
            return

        threading.Thread(
            target=self._wait_migrate_ready_and_broadcast,
            args=(target_uid, target_ip),
            daemon=True,
            name="migrate-wait-ready",
        ).start()

    def _wait_migrate_ready_and_broadcast(self, target_uid: int,
                                           target_ip: str) -> None:
        evt = self._pending_migrate_ready
        if evt is None:
            return

        got_ready = evt.wait(timeout=MIGRATE_PREPARE_TIMEOUT_SEC)

        with self._pending_migrate_lock:
            self._pending_migrate_target = None
            self._pending_migrate_ready  = None

        if got_ready:
            print(f"[Server] migrate: target uid={target_uid} READY — broadcast")
        else:
            print(f"[Server] migrate: TIMEOUT ожидания READY от uid={target_uid}")

        self._broadcast_server_migrate(target_uid, target_ip)

        time.sleep(0.35)

        with self.clients_lock:
            conns = list(self.clients.keys())
        for c in conns:
            try:
                c.shutdown(socket.SHUT_WR)
            except Exception:
                pass

        self._accepting = False
        try:
            self.tcp_sock.close()
        except Exception:
            pass
        try:
            self.udp_sock.close()
        except Exception:
            pass
        if self._announcer:
            try:
                self._announcer.stop()
            except Exception:
                pass
            self._announcer = None
        if self.sfu is not None:
            try:
                self.sfu.shutdown()
            except Exception:
                pass
            self.sfu = None

        print("[Server] migrate: сервер остановлен после передачи")

    def _handle_migrate_ready(self, uid: int) -> None:
        with self._pending_migrate_lock:
            pending = self._pending_migrate_target
            evt     = self._pending_migrate_ready

        if pending is None or pending != uid:
            print(f"[Server] migrate_ready от uid={uid} но "
                  f"pending_target={pending} — игнорируем")
            return
        if evt is not None:
            print(f"[Server] migrate_ready от uid={uid} — зелёный свет")
            evt.set()

    def _broadcast_server_migrate(self, new_host_uid: int, new_host_ip: str) -> None:
        payload = json.dumps({
            'action':       CMD_SERVER_MIGRATE,
            'new_host_uid': new_host_uid,
            'new_host_ip':  new_host_ip,
        }).encode('utf-8')

        with self.clients_lock:
            conns = list(self.clients.keys())

        # SENIOR FIX: _safe_send — при миграции broadcaster ещё активен,
        # иначе байты CMD_SERVER_MIGRATE могут перемешаться с sync_users.
        for c in conns:
            self._safe_send(c, payload)

    # ------------------------------------------------------------------
    # Запуск сервера (standalone режим)
    # ------------------------------------------------------------------
    def start(self):
        if self.sfu is None:
            try:
                from network_engine.sfu_bridge import get_shared as _get_sfu
                _sfu_bridge = _get_sfu()
                if not _sfu_bridge.is_running():
                    _sfu_bridge.start()
                if _sfu_bridge.is_running():
                    self.sfu = PionSfuProxy(sfu_bridge=_sfu_bridge)
                    print("[Server] Pion SFU подключён (standalone)")
            except ImportError:
                pass
            except Exception as e:
                print(f"[Server] SFU init error: {e}")

        threading.Thread(target=self.udp_handler,   daemon=True).start()
        threading.Thread(target=self.stats_monitor, daemon=True).start()
        print(f"[Server] Запущен. TCP:{DEFAULT_PORT_TCP}, UDP:{DEFAULT_PORT_UDP}")

        while True:
            conn, addr = self.tcp_sock.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(
                target=self.tcp_handler,
                args=(conn, addr),
                daemon=True,
            ).start()


# =============================================================================
# EmbeddedServerManager — менеджер встроенного сервера
# =============================================================================
class EmbeddedServerManager:
    """Singleton-менеджер встроенного SFUServer."""
    _instance: 'EmbeddedServerManager | None' = None
    _lock: threading.Lock = threading.Lock()

    def __init__(self):
        self._server = None

    @classmethod
    def get(cls) -> 'EmbeddedServerManager':
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def start(self, host_ip: str, host_nick: str, server_name: str = '') -> None:
        if self.is_running():
            print("[EmbeddedServer] Уже запущен — повторный запуск пропущен")
            return

        if not server_name:
            from config import USER_CONFIG_PATH
            try:
                if os.path.exists(USER_CONFIG_PATH):
                    with open(USER_CONFIG_PATH, 'r', encoding='utf-8') as f:
                        server_name = json.load(f).get('server_name', 'InPulse Server')
            except Exception:
                pass
            server_name = server_name or 'InPulse Server'

        self._server = SFUServer(server_name=server_name)
        try:
            self._server.start_embedded(host_ip, host_nick)
        except Exception:
            self._server = None
            raise
        print(f"[EmbeddedServer] Запущен: ip={host_ip}, nick={host_nick!r}, name={server_name!r}")

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.stop_gracefully()
            except Exception as e:
                print(f"[EmbeddedServer] Ошибка остановки: {e}")
            self._server = None

    def stop_silent(self) -> None:
        if self._server is not None:
            try:
                self._server.stop_silent()
            except Exception as e:
                print(f"[EmbeddedServer] stop_silent error: {e}")
            self._server = None

    def stop_announcer_only(self) -> None:
        if self._server is not None and self._server._announcer is not None:
            try:
                self._server._announcer.stop()
                self._server._announcer = None
                print("[EmbeddedServer] Анонсер остановлен (передача хостинга)")
            except Exception as e:
                print(f"[EmbeddedServer] stop_announcer_only error: {e}")

    def is_running(self) -> bool:
        return self._server is not None


if __name__ == "__main__":
    SFUServer().start()
