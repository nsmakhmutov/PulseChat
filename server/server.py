# server.py — SFUServer + EmbeddedServerManager
import asyncio
import json
import hashlib
import os
import secrets
import socket
import threading
import time

from config import (
    DEFAULT_PORT_TCP, DEFAULT_PORT_UDP, BUFFER_SIZE,
    UDP_RECV_BUFFER_SIZE, UDP_SEND_BUFFER_SIZE,
    UDP_HEADER_STRUCT, UDP_HEADER_SIZE,
    FLAG_STREAM_VOICES, FLAG_WHISPER,
    STREAM_VOICE_HEADER_STRUCT, STREAM_VOICE_HEADER_SIZE,
    CMD_LOGIN, CMD_JOIN_ROOM, CMD_STREAM_START, CMD_STREAM_STOP,
    CMD_SYNC_USERS, CMD_SOUNDBOARD,
    CMD_UPDATE_PRESENCE,
    CMD_NUDGE_VOTE, CMD_PLAY_NUDGE, CMD_NUDGE_TRIGGERED, NUDGE_COOLDOWN_SEC,
    CMD_FILE_OFFER, CMD_FILE_OFFER_ROOM,
    CMD_WEBRTC_OFFER, CMD_WEBRTC_ANSWER, CMD_WEBRTC_ICE,
    CMD_SERVER_TRANSFER, CMD_SERVER_MIGRATE,
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

        # --- UDP (только голос комнаты + ping) ---
        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # SO_RCVBUF 2MB: голосовые пакеты не дропаются пока handler занят.
        # SO_SNDBUF 2MB: исходящая очередь не блокирует recv-путь.
        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, UDP_RECV_BUFFER_SIZE)
        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, UDP_SEND_BUFFER_SIZE)
        self.udp_sock.bind((host, DEFAULT_PORT_UDP))

        # -------------------------------------------------------------------
        # Разделение локов (FIX #1):
        #   clients_lock  — self.clients (TCP-потоки)
        #   udp_lock      — self.udp_map (UDP-поток)
        #   watchers_lock — self.watchers (stream-события)
        # UDP-поток никогда не ждёт TCP sendall().
        # -------------------------------------------------------------------
        self.clients_lock  = threading.Lock()
        self.udp_lock      = threading.Lock()
        self.watchers_lock = threading.Lock()

        # conn → {nick, room, uid, avatar, ip, mute, deaf, is_streaming,
        #          status_icon, status_text}
        self.clients = {}

        # uid → addr  (UDP-адрес клиента)
        self.udp_map = {}

        # uid → room  (кэш для O(1) поиска в UDP-маршрутизации)
        self.uid_to_room = {}

        # streamer_uid → {watcher_uid: {nick, avatar, uid}}
        self.watchers = {}

        self.stats      = {"packets": 0, "bytes": 0}
        self.start_time = time.time()

        # --- Голосование «Пнуть» (Nudge) ---
        # { room_name → { target_uid → { voter_uid → vote_timestamp } } }
        self.nudge_votes = {}
        self.nudge_lock  = threading.Lock()

        # --- WebRTC SFU (создаётся в start()) ---
        self.sfu: PionSfuProxy | None = None

        # ── Кэш payload send_global_state (FIX 8) ───────────────────────────
        # send_global_state сериализует всех юзеров в JSON и шлёт всем клиентам.
        # При 20 юзерах: ~4 KB × 11+ вызовов/сек × 20 клиентов = ~880 KB/сек зря.
        # Решение: кэшируем bytes-payload и пересобираем только при реальных
        # изменениях состояния (_state_dirty=True).
        # Грязный флаг выставляется при каждом изменении clients/watchers/channels.
        # После сборки payload флаг сбрасывается — повторные вызовы отдают кэш.
        self._cached_payload: bytes | None = None
        self._state_dirty: bool = True   # True при старте → первый вызов всегда строит

        # ── Встроенный сервер: host_order ────────────────────────────────────
        # Упорядоченный список UID в порядке первого входа на сервер.
        # host_order[0] = потенциальный новый хост при падении текущего.
        # Рассылается клиентам в каждом sync_users — хранится локально
        # для авто-переключения при потере соединения.
        self._host_order: list[int]   = []
        self._host_order_lock         = threading.Lock()

        # ── Встроенный сервер: управление ────────────────────────────────────
        # _is_embedded=True: сервер запущен внутри процесса клиента (start_embedded).
        # _accepting: False → accept-цикл завершится после следующего accept().
        # _owner_ip: RadminVPN IP владельца сервера (кто вызвал start_embedded).
        #   При CMD_LOGIN клиент с этим IP вставляется в host_order[0] независимо
        #   от порядка подключения. Исправляет гонку: 150мс QTimer в _on_become_host
        #   даёт другим клиентам (Client3 через _migrate_reconnect) подключиться
        #   раньше владельца и захватить host_order[0] → права передачи улетают.
        self._is_embedded: bool       = False
        self._accepting:   bool       = True
        self._announcer               = None   # ServerAnnouncer | None
        self._owner_ip:    str        = ''     # IP создателя встроенного сервера

        # ── Имя сервера (отображается в списке серверов у клиентов) ──────────
        self._server_name = server_name or SERVER_NAME_DEFAULT

        # ── Пинги клиентов (для выбора следующего хоста по min RTT) ────────────
        # uid → ping_ms (EWMA RTT клиента к серверу, самостоятельно измеренный)
        # Обновляется при получении action='report_ping' от клиента.
        # Используется _pick_best_host() при передаче/миграции сервера.
        self._client_pings: dict[int, int] = {}
        self._client_pings_lock = threading.Lock()

        # ── Временные каналы ─────────────────────────────────────────────────
        # channel_name → {'password': str|None, 'permanent': bool}
        # FIX #4: имя главного канала читается из USER_CONFIG_PATH.
        # Хост может переименовать его через контекстное меню;
        # имя сохраняется в конфиге и применяется при каждом старте сервера.
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

        # Кэш авторизованных клиентов для защищённых каналов
        # conn → set[channel_name]
        self._channel_auth: dict = {}
        self._channel_auth_lock = threading.Lock()

        # ── Кэш медиа-пейлоадов (FIX #1) ────────────────────────────────────
        # json.dumps({'file_data_b64': <10MB>}) занимает ~50 мс на CPython.
        # При пересылке одного файла 10 получателям = 500 мс задержки в TCP-потоке.
        # Решение: кэшируем готовые bytes-пейлоады по MD5(file_data_b64).
        # Ключ: md5_hex строки. Значение: (expire_ts, payload_bytes_without_ts).
        # TTL = 300 сек, макс 30 записей — при 20 юзерах этого с запасом хватит.
        # ts подставляется при каждой отправке, поэтому кэш хранит payload без ts,
        # а финальные bytes строятся за O(len(ts_str)) вместо O(len(10MB)).
        self._media_cache: dict[str, tuple[float, bytes, bytes]] = {}
        # (expire_ts, prefix_bytes, suffix_bytes)
        # prefix = всё до "ts": в JSON, suffix = всё после значения ts
        self._media_cache_lock = threading.Lock()
        self._media_cache_max  = 30
        self._media_cache_ttl  = 300.0  # секунд

        # ── SQLite чат (хост хранит историю на диске) ────────────────────────
        self._chat_db: ChatDB | None = None
        try:
            self._chat_db = ChatDB()
            print(f"[Server] SQLite чат: {CHAT_DB_PATH}")
        except Exception as e:
            print(f"[Server] ChatDB init error: {e}")
            self._chat_db = None

    # ------------------------------------------------------------------
    # Управление каналами
    # ------------------------------------------------------------------
    def get_client_count(self) -> int:
        """Возвращает текущее число подключённых клиентов. Используется ServerAnnouncer."""
        with self.clients_lock:
            return len(self.clients)

    def _get_user_nicks_list(self) -> list:
        """Возвращает список никнеймов подключённых клиентов. Используется ServerAnnouncer
        для включения в UDP-broadcast, чтобы клиенты показывали hover-попап с участниками."""
        with self.clients_lock:
            return [c.get('nick', '') for c in self.clients.values() if c.get('nick')]

    def _pick_best_host(self, exclude_uid: int = 0) -> tuple[int, str]:
        """
        Выбирает лучшего кандидата в новые хосты по критерию минимального RTT.

        Алгоритм:
          1. Берём всех подключённых клиентов кроме exclude_uid (текущий хост).
          2. Сортируем по self._client_pings[uid] (чем меньше — тем лучше).
          3. Если у клиента нет данных о пинге — считаем его пинг = 999ms
             (хуже любого реального, но лучше "вообще нет кандидатов").
          4. При равенстве пинга приоритет отдаётся первому в host_order
             (они пришли раньше, сеть скорее всего надёжнее).

        Возвращает (uid, ip) лучшего кандидата или (0, '') если нет кандидатов.
        """
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
            # Вторичная сортировка по позиции в host_order (меньше = раньше пришёл)
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
        """Снимок списка каналов для включения в sync_users."""
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
        """Создаёт временный канал. True если создан, False если уже существует."""
        with self._channels_lock:
            if name in self._channels:
                return False
            self._channels[name] = {'password': password, 'permanent': False}
        print(f"[Server] 📢 Создан канал '{name}' (пароль: {'да' if password else 'нет'})")
        return True

    def _cleanup_temp_channels(self, leaving_room: str):
        """Удаляет пустой временный канал и рассылает CMD_CHANNEL_DELETED."""
        # FIX #6: исправлен дедлок вложенных локов.
        # Было: with _channels_lock → with clients_lock (порядок A→B).
        # tcp_handler держит clients_lock и берёт _channels_lock (порядок B→A).
        # Два потока = классический deadlock.
        #
        # Решение: трёхфазный подход без вложенности:
        #   1. Читаем канал под _channels_lock (не трогаем clients_lock).
        #   2. Считаем occupants под clients_lock (не трогаем _channels_lock).
        #   3. Удаляем и рассылаем под _channels_lock, затем clients_lock — строго по очереди.

        # Фаза 1: проверяем что канал существует и не постоянный
        with self._channels_lock:
            ch = self._channels.get(leaving_room)
            if not ch or ch['permanent']:
                return

        # Фаза 2: считаем occupants БЕЗ _channels_lock
        with self.clients_lock:
            occupants = sum(
                1 for c in self.clients.values()
                if c.get('room') == leaving_room
            )
        if occupants > 0:
            return

        # Фаза 3: удаляем канал (проверяем снова — канал мог измениться)
        with self._channels_lock:
            ch = self._channels.get(leaving_room)
            if not ch or ch['permanent']:
                return  # уже удалён или стал постоянным между фазами
            self._channels.pop(leaving_room, None)

        print(f"[Server] 🗑 Временный канал '{leaving_room}' удалён (пустой)")
        payload = json.dumps({
            'action':       CMD_CHANNEL_DELETED,
            'channel_name': leaving_room,
        }).encode('utf-8')
        with self.clients_lock:
            conns = list(self.clients.keys())
        for c in conns:
            try:
                c.sendall(payload)
            except Exception:
                pass

    def _check_channel_auth(self, conn, channel_name: str) -> bool:
        """True если conn авторизован для входа в channel_name."""
        with self._channels_lock:
            ch = self._channels.get(channel_name)
        if ch is None:
            return False
        if ch['password'] is None:
            return True
        with self._channel_auth_lock:
            return channel_name in self._channel_auth.get(conn, set())

    # ------------------------------------------------------------------
    # Вспомогательный метод: отправка JSON клиенту из любого потока
    # ------------------------------------------------------------------
    def send_to_conn(self, conn, msg: dict) -> None:
        """
        Синхронная отправка JSON клиенту.
        Используется из tcp_handler (threading-контекст).
        Для вызова из asyncio-контекста — используй SFU._send_async().
        """
        try:
            conn.sendall(json.dumps(msg).encode('utf-8'))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Мониторинг
    # ------------------------------------------------------------------
    def stats_monitor(self):
        last_bytes = 0
        while True:
            time.sleep(5)
            with self.clients_lock:
                active = len(self.clients)
            curr_bytes = self.stats["bytes"]   # int — атомарное чтение
            diff = (curr_bytes - last_bytes) / 1024 / 5
            print(f"[Stats] Active: {active} | Traffic: {diff:.1f} KB/s")
            last_bytes = curr_bytes

    # ------------------------------------------------------------------
    # UDP-маршрутизация (только голос + ping)
    # ------------------------------------------------------------------
    def udp_handler(self):
        """
        FIX #1 + FIX #2: UDP-поток держит лок только на минимальное время.
        Все sendto() выполняются после освобождения лока.

        После рефакторинга обрабатывает только:
          — Ping (flags=254): echo без локов.
          — Голос комнаты: broadcast всем в комнате кроме отправителя.
          — Whisper (FLAG_WHISPER): доставка конкретному получателю.
          — FLAG_STREAM_VOICES: голоса участников для зрителей стрима.
              (Mix Minus UDP — сохранён для первой итерации, WebRTC аудио позже)

        Видеопакеты (FLAG_VIDEO, FLAG_STREAM_AUDIO) больше не приходят через UDP —
        стрим передаётся через WebRTC.
        """
        while True:
            try:
                data, addr = self.udp_sock.recvfrom(BUFFER_SIZE)
                if len(data) < UDP_HEADER_SIZE:
                    continue

                sender_uid, msg_ts, seq, flags = UDP_HEADER_STRUCT.unpack(
                    data[:UDP_HEADER_SIZE]
                )

                # Ping: отвечаем немедленно, без локов
                if flags == 254:
                    self.udp_sock.sendto(data, addr)
                    continue

                # Обновляем UDP-адрес отправителя.
                # stats обновляем ВНЕ лока — int-запись атомарна через GIL.
                with self.udp_lock:
                    self.udp_map[sender_uid] = addr
                    sender_room = self.uid_to_room.get(sender_uid)
                self.stats["packets"] += 1
                self.stats["bytes"]   += len(data)

                if not sender_room:
                    continue

                is_stream_voices = bool(flags & FLAG_STREAM_VOICES)
                is_whisper       = bool(flags & FLAG_WHISPER)

                if is_whisper:
                    # ШЁПОТ → только target_uid.
                    # Payload: [target_uid: 4 байта big-endian] + [opus].
                    if len(data) < UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE:
                        continue
                    (target_uid,) = STREAM_VOICE_HEADER_STRUCT.unpack(
                        data[UDP_HEADER_SIZE: UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE]
                    )
                    with self.udp_lock:
                        target_addr = self.udp_map.get(target_uid)
                    if target_addr:
                        try:
                            self.udp_sock.sendto(data, target_addr)
                        except Exception:
                            pass

                elif is_stream_voices:
                    # ГОЛОСОВОЙ ПОТОК СТРИМА (Mix Minus) → зрители стримера.
                    # Payload: [speaker_uid: 4 байта] + [opus].
                    # Зритель получает полный пакет, отбрасывает свой speaker_uid.
                    # UDP-путь сохранён для первой итерации (план рекомендация В).
                    self._send_to_watchers(sender_uid, data)

                else:
                    # АУДИО КОМНАТЫ → все в той же комнате, кроме отправителя.
                    # FIX: без вложенных локов (clients_lock → udp_lock).
                    # 1. Под clients_lock собираем uid получателей.
                    # 2. Под udp_lock разрешаем uid → addr.
                    # 3. sendto() — без любых локов.
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

            except Exception:
                pass

    # ------------------------------------------------------------------
    # TCP-обработчик одного клиента
    # ------------------------------------------------------------------
    def tcp_handler(self, conn, addr):
        uid       = secrets.randbelow(10**9) + 1  # криптографически уникальный
        client_ip = addr[0]
        buffer    = ""
        # JSONDecoder создаём ОДИН РАЗ на соединение — он stateless.
        _decoder  = json.JSONDecoder()

        try:
            while True:
                chunk_bytes = conn.recv(4096)
                if not chunk_bytes:
                    break
                buffer += chunk_bytes.decode('utf-8', errors='ignore')

                while True:
                    try:
                        msg, idx = _decoder.raw_decode(buffer)
                        buffer   = buffer[idx:].lstrip()
                        action   = msg.get('action')

                        # ── Login ─────────────────────────────────────────────
                        if action == CMD_LOGIN:
                            client_nick   = msg.get('nick', 'User')
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
                            # Добавляем в очередь потенциальных хостов.
                            # FIX (Bug G): в embedded-режиме владелец сервера (_owner_ip)
                            # всегда вставляется в host_order[0], независимо от порядка
                            # подключения. 150мс QTimer в _on_become_host даёт другим
                            # клиентам подключиться раньше — без этого они захватывали
                            # host_order[0] и получали права передачи сервера.
                            with self._host_order_lock:
                                if uid not in self._host_order:
                                    if self._is_embedded and client_ip == self._owner_ip:
                                        self._host_order.insert(0, uid)
                                        print(f"[Server] host_order: владелец {client_nick} "
                                              f"(uid={uid}) → position 0 (приоритет по IP)")
                                    else:
                                        self._host_order.append(uid)
                            conn.sendall(
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

                            # Проверяем существование и пароль канала
                            with self._channels_lock:
                                ch = self._channels.get(new_room)

                            if new_room != _gcn_jr and ch is None:
                                conn.sendall(json.dumps({
                                    'action': 'join_room_denied',
                                    'reason': 'not_found',
                                    'room':   new_room,
                                }).encode('utf-8'))
                            elif ch and ch['password'] is not None and not self._check_channel_auth(conn, new_room):
                                conn.sendall(json.dumps({
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
                                conn.sendall(json.dumps({
                                    'action': 'channel_auth_result',
                                    'ok': False, 'reason': 'not_found',
                                    'channel_name': ch_name,
                                }).encode('utf-8'))
                            elif ch['password'] is None or ch['password'] == ch_pass:
                                with self._channel_auth_lock:
                                    self._channel_auth.setdefault(conn, set()).add(ch_name)
                                conn.sendall(json.dumps({
                                    'action': 'channel_auth_result',
                                    'ok': True,
                                    'channel_name': ch_name,
                                }).encode('utf-8'))
                            else:
                                conn.sendall(json.dumps({
                                    'action': 'channel_auth_result',
                                    'ok': False, 'reason': 'wrong_password',
                                    'channel_name': ch_name,
                                }).encode('utf-8'))

                        # ── Создание временного канала (только хост) ──────────
                        elif action == CMD_CREATE_CHANNEL:
                            with self._host_order_lock:
                                is_host = bool(self._host_order and self._host_order[0] == uid)
                            if not is_host:
                                conn.sendall(json.dumps({
                                    'action': 'create_channel_result',
                                    'ok': False, 'reason': 'not_host',
                                }).encode('utf-8'))
                            else:
                                ch_name = msg.get('channel_name', '').strip()[:CHANNEL_NAME_MAX_LEN]
                                ch_pass = msg.get('password', '').strip()[:CHANNEL_PASS_MAX_LEN] or None
                                if not ch_name or ch_name.lower() == 'general':
                                    conn.sendall(json.dumps({
                                        'action': 'create_channel_result',
                                        'ok': False, 'reason': 'invalid_name',
                                    }).encode('utf-8'))
                                elif self._create_temp_channel(ch_name, ch_pass):
                                    conn.sendall(json.dumps({
                                        'action': 'create_channel_result',
                                        'ok': True, 'channel_name': ch_name,
                                    }).encode('utf-8'))
                                    self._mark_dirty()
                                    self.send_global_state()
                                else:
                                    conn.sendall(json.dumps({
                                        'action': 'create_channel_result',
                                        'ok': False, 'reason': 'already_exists',
                                    }).encode('utf-8'))

                        # ── FIX #4: Переименование постоянного канала (только хост) ──
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
                                            # Переводим всех клиентов из старого имени в новое
                                            with self.clients_lock:
                                                for cdata in self.clients.values():
                                                    if cdata.get('room') == old_nm:
                                                        cdata['room'] = new_nm
                                            with self.udp_lock:
                                                for k, v in self.uid_to_room.items():
                                                    if v == old_nm:
                                                        self.uid_to_room[k] = new_nm
                                            self._general_channel_name = new_nm
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
                            # FIX #10: UDP-keepalive шлёт update_status каждую секунду
                            # на каждого пользователя — даже если mute/deaf не менялись.
                            # С 20 юзерами: 20 send_global_state/сек × (сериализация всех
                            # + 20 sendall) = 400 sendall/сек зря.
                            # Решение: сравниваем старое и новое значения; broadcast
                            # только если что-то реально изменилось.
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
                        # ── Ping Report (тихий — без send_global_state) ────────
                        # Клиент шлёт свой текущий RTT раз в ~3 сек из ping_loop.
                        # Сервер сохраняет для _pick_best_host (выбор нового хоста).
                        elif action == 'report_ping':
                            ping_ms = msg.get('ping_ms', 0)
                            if isinstance(ping_ms, (int, float)) and ping_ms >= 0:
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
                            with self.clients_lock:
                                if conn in self.clients:
                                    self.clients[conn]['is_streaming'] = True
                                    # Сохраняем реальный порт SFU стримера.
                                    # Клиент передаёт его в сообщении (динамический порт).
                                    self.clients[conn]['sfu_port'] = msg.get('sfu_port', 7788)
                                    print(f"[Server] {self.clients[conn]['nick']} запустил стрим (SFU порт={self.clients[conn]['sfu_port']})")
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
                                # Закрываем WebRTC сессию стримера и всех его зрителей
                                if self.sfu:
                                    self.sfu.close_streamer(stopped_uid)
                            self._mark_dirty()
                            self.send_global_state()

                        # ── Stream Watch Start ────────────────────────────────
                        elif action == 'stream_watch_start':
                            streamer_uid = msg.get('streamer_uid')
                            if streamer_uid is not None:
                                with self.clients_lock:
                                    if conn in self.clients:
                                        watcher        = self.clients[conn]
                                        w_uid          = watcher['uid']
                                        watcher_nick   = watcher['nick']
                                        watcher_avatar = watcher.get('avatar', '1.svg')
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
                                # WebRTC v3: посылаем зрителю триггер → он создаёт offer
                                if self.sfu:
                                    quality = msg.get('quality', 'hq')
                                    # Находим RadminVPN IP стримера для роутинга
                                    # viewer-оффера к его sidecar.exe (не-хост стримеры).
                                    streamer_ip = '127.0.0.1'
                                    with self.clients_lock:
                                        for _c in self.clients.values():
                                            if _c.get('uid') == streamer_uid:
                                                raw_ip = _c.get('ip', '127.0.0.1')
                                                # FIX БАГ 3: стример — владелец
                                                # embedded-сервера. Его IP хранится как
                                                # RadminVPN-адрес (26.x.x.x), но SFU
                                                # слушает только на localhost.
                                                # Если IP == _owner_ip → 127.0.0.1.
                                                if self._is_embedded and raw_ip == self._owner_ip:
                                                    streamer_ip = '127.0.0.1'
                                                else:
                                                    streamer_ip = raw_ip
                                                streamer_sfu_port = _c.get('sfu_port', 7788)
                                                break
                                    self.sfu.trigger_viewer_connect(
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
                                    # Закрываем WebRTC PC зрителя
                                    if self.sfu:
                                        self.sfu.close_viewer(w_uid)
                            self._mark_dirty()
                            self.send_global_state()

                        # ── WebRTC Offer ──────────────────────────────────────
                        elif action == CMD_WEBRTC_OFFER:
                            sdp      = msg.get('sdp')
                            sdp_type = msg.get('type', 'offer')
                            role     = msg.get('role', '')

                            if not self.sfu:
                                continue

                            print(f"[Server] CMD_WEBRTC_OFFER: role={role!r}, uid={uid}, sdp_len={len(sdp) if sdp else 0}, sfu={bool(self.sfu)}")
                            if role == 'viewer_offer':
                                # v3: зритель прислал свой offer → прокси в Pion SFU
                                if sdp:
                                    print(f"[Server] → handle_viewer_offer(uid={uid})")
                                    self.sfu.handle_viewer_offer(uid, sdp, conn)
                                else:
                                    print(f"[Server] ❌ viewer_offer с пустым SDP от uid={uid}")
                            elif role == 'streamer':
                                print(f"[Server] CMD_WEBRTC_OFFER role=streamer uid={uid}: ignored in v3")
                            elif role == 'viewer':
                                print(f"[Server] CMD_WEBRTC_OFFER role=viewer uid={uid}: триггер от старого клиента (без v3 offer)")
                            else:
                                print(f"[Server] CMD_WEBRTC_OFFER неизвестный role={role!r} uid={uid}")

                        # ── WebRTC Answer (v3: не используется — сервер сам отвечает) ──
                        elif action == CMD_WEBRTC_ANSWER:
                            pass  # v3: answer идёт server→viewer, не viewer→server

                        # ── WebRTC ICE Candidate (v3: gather-complete — trickle не используется)
                        elif action == CMD_WEBRTC_ICE:
                            pass  # v3: gather-complete ICE, кандидаты в SDP

                        # ── Передача сервера другому участнику ────────────────
                        elif action == CMD_SERVER_TRANSFER:
                            target_uid_st = msg.get('target_uid')
                            if not isinstance(target_uid_st, int):
                                continue
                            # Только хост (host_order[0]) может передать сервер
                            with self._host_order_lock:
                                is_host = bool(
                                    self._host_order and self._host_order[0] == uid
                                )
                            if not is_host:
                                print(
                                    f"[Server] server_transfer от uid={uid}: "
                                    f"не является хостом — отклонено"
                                )
                                continue

                            # target_uid == 0 → авто-выбор по минимальному пингу
                            if target_uid_st == 0:
                                target_uid_st, target_ip_st = self._pick_best_host(
                                    exclude_uid=uid
                                )
                                if not target_uid_st:
                                    print("[Server] server_transfer auto: нет кандидатов")
                                    continue
                                target_nick_st = '?'
                                with self.clients_lock:
                                    for c_data in self.clients.values():
                                        if c_data['uid'] == target_uid_st:
                                            target_nick_st = c_data.get('nick', '?')
                                            break
                            else:
                                # Конкретный uid — ищем его IP
                                target_ip_st   = None
                                target_nick_st = '?'
                                with self.clients_lock:
                                    for c_data in self.clients.values():
                                        if c_data['uid'] == target_uid_st:
                                            target_ip_st   = c_data.get('ip')
                                            target_nick_st = c_data.get('nick', '?')
                                            break
                                if not target_ip_st:
                                    print(
                                        f"[Server] server_transfer: "
                                        f"uid={target_uid_st} не найден"
                                    )
                                    continue
                            print(
                                f"[Server] 🔀 Передача сервера: "
                                f"uid={uid} → {target_nick_st} (uid={target_uid_st}, "
                                f"IP={target_ip_st})"
                            )
                            self._broadcast_server_migrate(
                                target_uid_st, target_ip_st
                            )

                        # ── Soundboard ────────────────────────────────────────
                        elif action == CMD_SOUNDBOARD:
                            with self.clients_lock:
                                sender_nick = self.clients[conn]['nick'] if conn in self.clients else '?'
                                conns = list(self.clients.keys())
                            msg['from_nick'] = sender_nick
                            payload = json.dumps(msg).encode('utf-8')
                            # FIX #2: sendall вне clients_lock
                            for c in conns:
                                try:
                                    c.sendall(payload)
                                except Exception:
                                    pass

                        # ── Nudge Vote ────────────────────────────────────────
                        elif action == CMD_NUDGE_VOTE:
                            target_uid = msg.get('target_uid')
                            if not isinstance(target_uid, int):
                                continue

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
                                    continue
                                voter_info  = self.clients[conn]
                                voter_uid_v = voter_info['uid']
                                voter_room  = voter_info['room']
                                voter_nick  = voter_info['nick']

                                room_uids = [
                                    c['uid'] for c in self.clients.values()
                                    if c['room'] == voter_room
                                ]
                                # Большинство голосующих (все кроме цели).
                                # 5 чел → 2 голоса, 4 → 2, 3 → 1, 2 → 1.
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
                                continue

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
                                    continue

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
                                try:
                                    t_conn.sendall(
                                        json.dumps({'action': CMD_PLAY_NUDGE}).encode('utf-8')
                                    )
                                    print(f"[Server] 👟 NUDGE FIRED → {target_nick}")
                                except Exception:
                                    pass

                                broadcast_payload = json.dumps({
                                    'action':      CMD_NUDGE_TRIGGERED,
                                    'target_nick': target_nick,
                                    'voter_nick':  voter_nick,
                                }).encode('utf-8')
                                for bc in broadcaster_conns:
                                    try:
                                        bc.sendall(broadcast_payload)
                                    except Exception:
                                        pass

                        # ── Файловая передача P2P ─────────────────────────────
                        elif action == CMD_FILE_OFFER:
                            target_uid_fo = msg.get('target_uid')
                            if not isinstance(target_uid_fo, int):
                                pass
                            else:
                                msg['sender_ip'] = client_ip
                                payload_fo = json.dumps(msg).encode('utf-8')
                                target_conn_fo = None
                                with self.clients_lock:
                                    for c_conn, c_data in self.clients.items():
                                        if c_data['uid'] == target_uid_fo:
                                            target_conn_fo = c_conn
                                            break
                                if target_conn_fo:
                                    try:
                                        target_conn_fo.sendall(payload_fo)
                                        print(
                                            f"[Server] 📁 file_offer: "
                                            f"uid={uid} → uid={target_uid_fo}"
                                        )
                                    except Exception:
                                        pass

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
                            for rc in room_conns:
                                try:
                                    rc.sendall(payload_for)
                                except Exception:
                                    pass
                            if room_conns:
                                print(
                                    f"[Server] 📁 file_offer_room: "
                                    f"uid={uid} → {len(room_conns)} получателей"
                                )

                        # ── Быстрый чат ───────────────────────────────────────
                        elif action == CMD_QUICK_MSG:
                            text = str(msg.get('text', '')).strip()[:QUICK_MSG_MAX_LEN]
                            if not text:
                                pass
                            else:
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
                                broadcast_qm = json.dumps({
                                    'action':     CMD_QUICK_MSG,
                                    'uid':        sender_uid_qm,
                                    'from_nick':  sender_nick,
                                    'text':       text,
                                }).encode('utf-8')
                                for bc in room_conns_qm:
                                    try:
                                        bc.sendall(broadcast_qm)
                                    except Exception:
                                        pass

                        # ── Постоянный чат: новое сообщение ───────────────────
                        # Сервер добавляет nick/uid/avatar/ts и рассылает всем
                        # в комнате включая отправителя (единый порядок у всех).
                        elif action == CMD_CHAT_MSG:
                            text_cm = str(msg.get('text', '')).strip()[:CHAT_MSG_MAX_LEN]
                            if text_cm:
                                with self.clients_lock:
                                    if conn in self.clients:
                                        c = self.clients[conn]
                                        sender_nick_cm   = c['nick']
                                        sender_uid_cm    = c['uid']
                                        sender_avatar_cm = c.get('avatar', '')
                                        sender_room_cm   = c.get('room')
                                    else:
                                        sender_nick_cm = sender_uid_cm = sender_room_cm = None
                                        sender_avatar_cm = ''
                                    if sender_room_cm:
                                        room_conns_cm = [
                                            c_conn
                                            for c_conn, c_data in self.clients.items()
                                            if c_data.get('room') == sender_room_cm
                                        ]
                                    else:
                                        room_conns_cm = []
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
                                    # SQLite: сохраняем на диске
                                    if self._chat_db:
                                        self._chat_db.add_message({
                                            'uid': sender_uid_cm, 'nick': sender_nick_cm,
                                            'avatar': sender_avatar_cm, 'text': text_cm,
                                            'room': sender_room_cm or '', 'ts': ts_cm,
                                        })
                                    for bc in room_conns_cm:
                                        try:
                                            bc.sendall(broadcast_cm)
                                        except Exception:
                                            pass

                        # ── Постоянный чат: запрос истории (SQLite) ──────────
                        # Сервер читает из SQLite напрямую, не relay к хосту.
                        elif action == CMD_CHAT_HISTORY_REQ:
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
                                    try:
                                        conn.sendall(json.dumps({
                                            'action':   CMD_CHAT_HISTORY,
                                            'messages': messages_db,
                                        }).encode('utf-8'))
                                    except Exception:
                                        pass

                        # ── Постоянный чат: relay истории (совместимость) ─────
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
                                    try:
                                        target_conn_ch.sendall(json.dumps({
                                            'action':   CMD_CHAT_HISTORY,
                                            'messages': messages_ch,
                                        }).encode('utf-8'))
                                    except Exception:
                                        pass

                        # ── Typing indicator ──────────────────────────────────
                        elif action == CMD_TYPING:
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
                                else:
                                    t_conns = []
                            for tc in t_conns:
                                try:
                                    tc.sendall(t_payload)
                                except Exception:
                                    pass

                        # ── Постоянный чат: медиа-вложение (фото/файл) ────────
                        # FIX #1: кэш пейлоадов по MD5(file_data_b64).
                        # json.dumps 10 MB base64 = ~50 мс на CPython.
                        # Тот же файл, пересланный снова (или при ретрансляции),
                        # отдаётся из кэша за ~0.1 мс.
                        # Кэш хранит (prefix_bytes, suffix_bytes):
                        #   prefix = JSON до поля "ts"
                        #   suffix = JSON после значения "ts"
                        # При отправке: prefix + str(ts).encode() + suffix
                        # → аллокация O(len(ts)) вместо O(10 MB).
                        elif action == CMD_CHAT_MEDIA:
                            file_data_b64 = msg.get('file_data_b64', '')
                            if (file_data_b64
                                    and len(file_data_b64) <= CHAT_MEDIA_MAX_B64):
                                with self.clients_lock:
                                    if conn in self.clients:
                                        c = self.clients[conn]
                                        s_nick_md   = c['nick']
                                        s_uid_md    = c['uid']
                                        s_avatar_md = c.get('avatar', '')
                                        s_room_md   = c.get('room')
                                    else:
                                        s_nick_md = s_uid_md = s_room_md = None
                                        s_avatar_md = ''
                                    if s_room_md:
                                        room_conns_md = [
                                            c_conn
                                            for c_conn, c_data in self.clients.items()
                                            if c_data.get('room') == s_room_md
                                        ]
                                    else:
                                        room_conns_md = []
                                if s_uid_md:
                                    now_ts = time.time()
                                    # ── Кэш: строим или берём готовый пейлоад ──
                                    md5_key = hashlib.md5(
                                        file_data_b64.encode('utf-8'), usedforsecurity=False
                                    ).hexdigest()
                                    with self._media_cache_lock:
                                        cached = self._media_cache.get(md5_key)
                                        hit = (cached is not None
                                               and cached[0] > now_ts
                                               and cached[3] == s_uid_md
                                               and cached[4] == s_nick_md)
                                    if hit:
                                        _, prefix_b, suffix_b, _, _ = cached
                                        broadcast_md = (
                                            prefix_b
                                            + f'{now_ts}'.encode('ascii')
                                            + suffix_b
                                        )
                                    else:
                                        # Полная сборка: строим payload и кэшируем части
                                        full_dict = {
                                            'action':        CMD_CHAT_MEDIA,
                                            'uid':           s_uid_md,
                                            'from_nick':     s_nick_md,
                                            'avatar':        s_avatar_md,
                                            'ts':            now_ts,
                                            'room':          s_room_md or '',
                                            'file_name':     msg.get('file_name', 'file'),
                                            'file_type':     msg.get('file_type', ''),
                                            'file_data_b64': file_data_b64,
                                        }
                                        broadcast_md = json.dumps(full_dict).encode('utf-8')
                                        # Разбиваем на prefix/suffix по полю "ts"
                                        # Формат json.dumps гарантирован: "ts": <float>
                                        try:
                                            ts_marker = f'"ts": {now_ts}'.encode('ascii')
                                            split_idx = broadcast_md.index(ts_marker)
                                            prefix_b = broadcast_md[:split_idx + 6]  # до числа
                                            suffix_b = broadcast_md[split_idx + 6 + len(f'{now_ts}'.encode('ascii')):]
                                            with self._media_cache_lock:
                                                # Вытесняем истёкшие записи если кэш полный
                                                if len(self._media_cache) >= self._media_cache_max:
                                                    expired = [k for k, v in self._media_cache.items()
                                                               if v[0] <= now_ts]
                                                    for k in expired:
                                                        del self._media_cache[k]
                                                    # Если всё ещё полный — удаляем самый старый
                                                    if len(self._media_cache) >= self._media_cache_max:
                                                        oldest = min(self._media_cache,
                                                                     key=lambda k: self._media_cache[k][0])
                                                        del self._media_cache[oldest]
                                                self._media_cache[md5_key] = (
                                                    now_ts + self._media_cache_ttl,
                                                    prefix_b, suffix_b,
                                                    s_uid_md, s_nick_md,
                                                )
                                        except (ValueError, Exception):
                                            pass  # кэш не удался — broadcast_md уже готов
                                    for bc in room_conns_md:
                                        try:
                                            bc.sendall(broadcast_md)
                                        except Exception:
                                            pass
                                    # SQLite: сохраняем медиа на диске
                                    if self._chat_db:
                                        self._chat_db.add_media({
                                            'uid': s_uid_md, 'nick': s_nick_md,
                                            'avatar': s_avatar_md,
                                            'room': s_room_md or '', 'ts': now_ts,
                                            'file_name': msg.get('file_name', 'file'),
                                            'file_type': msg.get('file_type', ''),
                                            'file_data_b64': file_data_b64,
                                        })

                        # ── Хост выключает микрофон участника ─────────────────
                        # Только mic off — уши не трогаются.
                        # Участник может включить mic обратно сам в любой момент.
                        # Сервер не меняет clients[conn]['mute'] — клиент сам
                        # отправит update_status после применения mute.
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
                                        try:
                                            target_conn_hm.sendall(json.dumps({
                                                'action': CMD_FORCE_MUTED,
                                            }).encode('utf-8'))
                                        except Exception:
                                            pass

                        # ── Draw Stroke: ретрансляция зрителям + стримеру ────────
                        # Зритель нарисовал мазок → сервер рассылает его всем
                        # остальным зрителям этого стрима и самому стримеру.
                        # points — нормализованные (0.0–1.0) координаты кадра.
                        # width зажат в [1,8], число точек ограничено DRAW_MAX_POINTS.
                        elif action == CMD_DRAW_STROKE:
                            streamer_uid_dr = msg.get('streamer_uid')
                            points_dr       = msg.get('points', [])
                            color_dr        = str(msg.get('color', '#FF6B6B'))[:16]
                            width_dr        = max(1, min(8, int(msg.get('width', 3))))
                            nick_dr         = str(msg.get('nick', '?'))[:32]

                            if streamer_uid_dr and isinstance(points_dr, list) and points_dr:
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

                                # Все зрители данного стрима
                                with self.watchers_lock:
                                    watcher_uids_dr = set(
                                        self.watchers.get(streamer_uid_dr, {}).keys()
                                    )

                                with self.clients_lock:
                                    # Зрители (включая отправителя для эха)
                                    target_conns_dr = [
                                        c for c, d in self.clients.items()
                                        if d.get('uid') in watcher_uids_dr
                                    ]
                                    # Стример — видит мазки у себя на экране
                                    streamer_conn_dr = next(
                                        (c for c, d in self.clients.items()
                                         if d.get('uid') == streamer_uid_dr),
                                        None,
                                    )
                                    if streamer_conn_dr:
                                        target_conns_dr.append(streamer_conn_dr)

                                for tc in target_conns_dr:
                                    try:
                                        tc.sendall(relay_dr)
                                    except Exception:
                                        pass

                    except json.JSONDecodeError:
                        break

        except Exception as e:
            # OSError (10054/10053 — разрыв соединения) — штатно, не логируем.
            # Остальные исключения логируем для диагностики.
            err_code = getattr(e, 'winerror', None) or getattr(e, 'errno', None)
            is_disconnect = err_code in (10054, 10053, 104, 32)
            if not is_disconnect:
                print(f"[Server] TCP ошибка: {e}")

        finally:
            # ── Очистка при отключении ────────────────────────────────────
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

                # Удаляем из очереди хостов
                with self._host_order_lock:
                    if u_id in self._host_order:
                        self._host_order.remove(u_id)

                # Очищаем auth-кэш отключившегося клиента
                with self._channel_auth_lock:
                    self._channel_auth.pop(conn, None)

                # FIX #11: очищаем RTT-пинг отключившегося клиента.
                # _client_pings[uid] никогда не удалялся при дисконнекте —
                # при 20 пользователях, которые периодически входят/выходят,
                # словарь рос бесконечно. Для приложения с максимум 20 юзерами
                # это незначительно по объёму, но семантически некорректно:
                # _pick_best_host мог учитывать RTT давно отключившегося клиента.
                with self._client_pings_lock:
                    self._client_pings.pop(u_id, None)

                # FIX 10: удаляем голоса отключившегося из nudge_votes.
                # Без этого voter_uid записи и target_uid-цели накапливались вечно.
                # Голоса хранятся внутри: {room → {target_uid → {voter_uid → ts}}}.
                # Удаляем u_id как voter и как target во всех комнатах.
                with self.nudge_lock:
                    for r_votes in self.nudge_votes.values():
                        # Как target — удаляем всю группу голосов за него
                        r_votes.pop(u_id, None)
                        # Как voter — удаляем его голос у каждой цели
                        for target_votes in r_votes.values():
                            target_votes.pop(u_id, None)

                # Удаляем временные каналы если они опустели
                if room and room != 'General':
                    self._cleanup_temp_channels(room)

                # Убираем пользователя из списков зрителей всех стримеров
                with self.watchers_lock:
                    for s_uid in list(self.watchers.keys()):
                        if u_id in self.watchers[s_uid]:
                            self.watchers[s_uid].pop(u_id, None)
                    self.watchers.pop(u_id, None)

                # Закрываем WebRTC сессии отключившегося пользователя
                if self.sfu:
                    # v3: PionSfuProxy — синхронные вызовы, нет call_async
                    self.sfu.close_streamer(u_id)   # no-op если не был стримером
                    self.sfu.close_viewer(u_id)     # no-op если не был зрителем

                print(
                    f"[Server] ✖ {nick} (комната: {room}) "
                    f"отключился | Онлайн: {remaining}"
                )
            else:
                print(f"[Server] ✖ Незарегистрированный клиент {addr[0]} отключился")

            conn.close()
            self._mark_dirty()
            self.send_global_state()

    # ------------------------------------------------------------------
    # Вспомогательный метод: отправка пакета всем зрителям стримера (UDP)
    # ------------------------------------------------------------------
    def _send_to_watchers(self, sender_uid: int, data: bytes):
        """
        Отправляет UDP-пакет всем зрителям стримера sender_uid.
        Используется для FLAG_STREAM_VOICES (Mix Minus, UDP-путь).
        Порядок локов: watchers_lock → udp_lock. sendto() вне любых локов.
        """
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
    # Рассылка глобального состояния по TCP
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Кэш состояния
    # ------------------------------------------------------------------
    def _mark_dirty(self) -> None:
        """Помечает кэш payload как устаревший. Вызывать при каждом изменении состояния."""
        self._state_dirty = True

    # ------------------------------------------------------------------
    # Рассылка глобального состояния по TCP
    # ------------------------------------------------------------------
    def send_global_state(self):
        """
        FIX 8: кэшированная версия — payload пересобирается только при изменениях.

        Было: json.dumps(~4KB) × 11+ вызовов/сек × 20 клиентов = ~880 KB/сек сериализации.
        Стало: при отсутствии изменений (update_status без реального изменения,
        повторные вызовы) — используем bytes-кэш без аллокации и сериализации.

        _state_dirty выставляется через _mark_dirty() при каждом реальном изменении
        clients / watchers / channels / host_order.
        """
        # ── Шаг 1: строим payload только если состояние изменилось ───────────
        if self._state_dirty:
            with self.watchers_lock:
                watchers_snapshot = {uid: dict(ws) for uid, ws in self.watchers.items()}

            with self._host_order_lock:
                host_order_snapshot = list(self._host_order)
                server_host_uid = self._host_order[0] if self._host_order else 0

            with self.clients_lock:
                state = {}
                conns_snapshot = []
                for c_conn, c in self.clients.items():
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
                    conns_snapshot.append(c_conn)

            self._cached_payload = json.dumps(
                {'action': CMD_SYNC_USERS, 'all_users': state,
                 'host_order': host_order_snapshot,
                 'server_host_uid': server_host_uid,
                 'channel_list': self._get_channel_list()}
            ).encode('utf-8')
            self._state_dirty = False

        else:
            # Кэш актуален — только снимаем список соединений
            with self.clients_lock:
                conns_snapshot = list(self.clients.keys())

        payload = self._cached_payload
        if payload is None:
            return

        # ── Шаг 2: рассылаем (всегда, даже при кэше) ─────────────────────────
        for c_conn in conns_snapshot:
            try:
                c_conn.sendall(payload)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Встроенный сервер: запуск внутри процесса клиента
    # ------------------------------------------------------------------
    def start_embedded(self, host_ip: str, host_nick: str) -> None:
        """
        Запускает сервер в фоновых потоках (не блокирует вызывающий поток).

        Отличие от start():
          — Возвращает управление немедленно после запуска потоков.
          — TCP accept-цикл работает в отдельном daemon-потоке.
          — Запускает ServerAnnouncer: broadcast в RadminVPN каждые
            DISCOVERY_INTERVAL секунд, чтобы другие клиенты нашли нас.

        Используется DiscoveryScreen и _on_become_host() в MainWindow.

        host_ip   — RadminVPN IP этого клиента (26.x.x.x), объявляется через broadcast.
        host_nick — ник пользователя, отображается в DiscoveryScreen других.
        """
        self._is_embedded = True
        self._accepting   = True

        # Инициализируем SQLite чат при запуске встроенного сервера
        if self._chat_db is None:
            try:
                self._chat_db = ChatDB()
                print("[Server] ChatDB инициализирован")
            except Exception as e:
                print(f"[Server] ChatDB init error: {e}")
        self._owner_ip    = host_ip   # FIX: сохраняем IP владельца для приоритета в host_order

        # Pion SFU Proxy (v3) — используем singleton чтобы не плодить sidecar.exe
        try:
            from sfu_bridge import get_shared as _get_sfu
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
        except ImportError:
            print("[Server] SfuBridge не найден — WebRTC недоступен")

        # Фоновые потоки сервера
        threading.Thread(target=self.udp_handler,   daemon=True, name="srv-udp").start()
        threading.Thread(target=self.stats_monitor, daemon=True, name="srv-stats").start()
        threading.Thread(
            target=self._embedded_accept_loop,
            daemon=True,
            name="srv-accept",
        ).start()

        # Запускаем broadcast-анонс
        try:
            from server_discovery import ServerAnnouncer
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
        """
        TCP accept-цикл для встроенного режима (неблокирующий вариант start()).
        Проверяет self._accepting после каждого accept() для корректного завершения.
        """
        self.tcp_sock.settimeout(1.0)   # таймаут чтобы иногда проверять _accepting
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
                continue   # проверяем _accepting снова
            except OSError:
                break      # сокет закрыт — завершаем

    def stop_gracefully(self) -> None:
        """
        Корректная остановка встроенного сервера с передачей хостинга.

        Алгоритм:
          1. Выбираем лучшего кандидата по RTT (_pick_best_host).
          2. Если нет никого — просто закрываемся.
          3. Broadcast CMD_SERVER_MIGRATE.
          4. sleep(0.35с) — TCP_NODELAY: пакет улетает за < 10мс, 350мс запас.
          5. shutdown(SHUT_WR) на каждый клиентский сокет — посылает TCP FIN.
             Клиент дочитывает CMD_SERVER_MIGRATE из буфера, затем получает EOF.
             БЕЗ этого: process-exit → ОС посылает TCP RST → Windows стирает
             непрочитанный буфер → клиент не видит CMD_SERVER_MIGRATE →
             _on_connection_lost → 4×3с=12с ожидания (Bug C).
          6. Закрываем listening-сокеты.
        """
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
            self.tcp_sock.close()
            self.udp_sock.close()
            return

        print(f"[Server] stop_gracefully: передаём хостинг uid={next_uid} ({next_ip})")
        self._broadcast_server_migrate(next_uid, next_ip)

        import time as _time
        _time.sleep(0.35)

        # FIX: SHUT_WR посылает FIN (graceful half-close) вместо RST при выходе.
        # Клиент успевает прочитать CMD_SERVER_MIGRATE из recv-буфера.
        with self.clients_lock:
            conns = list(self.clients.keys())
        for c in conns:
            try:
                c.shutdown(socket.SHUT_WR)
            except Exception:
                pass

        self.tcp_sock.close()
        self.udp_sock.close()

        # Останавливаем WebRTC SFU (закрывает все PC и asyncio loop)
        if self.sfu is not None:
            try:
                self.sfu.shutdown()
            except Exception as e:
                print(f"[Server] SFU shutdown error: {e}")
            self.sfu = None

        print("[Server] Встроенный сервер остановлен")

    def stop_silent(self) -> None:
        """
        Немедленная тихая остановка без broadcast CMD_SERVER_MIGRATE.

        Используется в двух случаях:
          1. _on_server_migrating: мы (как хост) уже разослали CMD_SERVER_MIGRATE
             через сервер, теперь надо закрыть свой embedded server.
          2. Когда мы сами получили CMD_SERVER_MIGRATE и нужно освободить порты
             до того как новый хост захочет стать хостом снова.

        НЕ вызывает _broadcast_server_migrate — миграция уже обработана.
        НЕ спит — мгновенная операция.

        Закрывает ВСЕ сокеты (Bug E: stop_announcer_only оставлял их открытыми):
          - listening TCP/UDP → порты 5000/5001 освобождены для нового SFUServer
          - принятые клиентские сокеты → _accepting цикл выходит
        Устанавливает mgr._server=None → is_running()=False → start() сработает.
        """
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

        # Останавливаем WebRTC SFU
        if self.sfu is not None:
            try:
                self.sfu.shutdown()
            except Exception as e:
                print(f"[Server] SFU shutdown error: {e}")
            self.sfu = None

        print("[Server] stop_silent: готово")

    def _broadcast_server_migrate(self, new_host_uid: int, new_host_ip: str) -> None:
        """
        Рассылает CMD_SERVER_MIGRATE всем подключённым клиентам.

        Клиенты-не-хосты reconnect к new_host_ip.
        Клиент с new_host_uid стартует встроенный сервер у себя.
        """
        payload = json.dumps({
            'action':       CMD_SERVER_MIGRATE,
            'new_host_uid': new_host_uid,
            'new_host_ip':  new_host_ip,
        }).encode('utf-8')

        with self.clients_lock:
            conns = list(self.clients.keys())

        for c in conns:
            try:
                c.sendall(payload)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Запуск сервера (standalone режим — без изменений)
    # ------------------------------------------------------------------
    def start(self):
        # Pion SFU Proxy (v3) — переподключение (singleton)
        if self.sfu is None:
            try:
                from sfu_bridge import get_shared as _get_sfu
                _sfu_bridge = _get_sfu()
                if not _sfu_bridge.is_running():
                    _sfu_bridge.start()
                if _sfu_bridge.is_running():
                    self.sfu = PionSfuProxy(sfu_bridge=_sfu_bridge)
                    print("[Server] Pion SFU подключён (переподключение)")
            except ImportError:
                pass

        threading.Thread(target=self.udp_handler,   daemon=True).start()
        threading.Thread(target=self.stats_monitor, daemon=True).start()
        print(f"[Server] Запущен. TCP:{DEFAULT_PORT_TCP}, UDP:{DEFAULT_PORT_UDP}")

        while True:
            conn, addr = self.tcp_sock.accept()
            # Отключаем алгоритм Нейгла: мелкие команды (nudge, stream events)
            # отправляются немедленно без буферизации.
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
    """
    Singleton-менеджер встроенного SFUServer внутри процесса клиента.
    Вынесен сюда для предотвращения двойного импорта (__main__ vs module).
    """
    _instance: 'EmbeddedServerManager | None' = None
    # FIX #12: лок для потокобезопасного singleton.
    # Без него два потока (например auto-host и UI) могут одновременно
    # войти в get() при _instance is None и создать два экземпляра.
    # При 20 юзерах вероятность невысока, но последствия — двойной bind
    # на один и тот же TCP/UDP порт → OSError: address already in use.
    _lock: threading.Lock = threading.Lock()

    def __init__(self):
        self._server = None  # SFUServer | None

    @classmethod
    def get(cls) -> 'EmbeddedServerManager':
        # FIX #12: double-checked locking — быстрый путь без лока если уже создан.
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
            import os, json
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