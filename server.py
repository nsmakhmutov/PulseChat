import socket
import threading
import json
import time
import secrets
from config import (
    DEFAULT_PORT_TCP, DEFAULT_PORT_UDP, BUFFER_SIZE,
    UDP_RECV_BUFFER_SIZE, UDP_SEND_BUFFER_SIZE,
    UDP_HEADER_STRUCT, UDP_HEADER_SIZE, FLAG_VIDEO, FLAG_STREAM_AUDIO,
    CMD_LOGIN, CMD_JOIN_ROOM, CMD_STREAM_START, CMD_STREAM_STOP,
    CMD_SYNC_USERS, CMD_SOUNDBOARD, FLAG_LOOPBACK_AUDIO, FLAG_STREAM_VOICES,
    FLAG_WHISPER, STREAM_VOICE_HEADER_STRUCT, STREAM_VOICE_HEADER_SIZE,
    CMD_UPDATE_PRESENCE,
    CMD_NUDGE_VOTE, CMD_PLAY_NUDGE, CMD_NUDGE_TRIGGERED, NUDGE_COOLDOWN_SEC,
    CMD_BITRATE_FEEDBACK, CMD_ADJUST_BITRATE, ABR_TIERS,
    FLAG_VIDEO_LQ, ABR_LQ_THRESHOLD,
)


class SFUServer:
    def __init__(self, host='0.0.0.0'):
        # --- TCP ---
        self.tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp_sock.bind((host, DEFAULT_PORT_TCP))
        self.tcp_sock.listen()

        # --- UDP ---
        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # FIX #4: Увеличены буферы ядра.
        # SO_RCVBUF: 8 MB — пакеты не дропаются пока handler занят маршрутизацией.
        # SO_SNDBUF: 8 MB — исходящая очередь не блокирует recv-путь.
        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, UDP_RECV_BUFFER_SIZE)
        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, UDP_SEND_BUFFER_SIZE)
        self.udp_sock.bind((host, DEFAULT_PORT_UDP))

        # -------------------------------------------------------------------
        # FIX #1: Разделение локов.
        #
        # Было: один self.lock на всё — UDP-поток ждал, пока TCP-поток
        #       делает sendall() внутри send_global_state(), что блокировало
        #       приём пакетов на сотни миллисекунд → пинг 1000 мс.
        #
        # Стало:
        #   self.clients_lock  — только для self.clients (TCP-потоки)
        #   self.udp_lock      — только для self.udp_map (UDP-поток)
        #   self.watchers_lock — только для self.watchers (stream-события)
        #
        # UDP-поток теперь никогда не ждёт TCP sendall().
        # -------------------------------------------------------------------
        self.clients_lock  = threading.Lock()
        self.udp_lock      = threading.Lock()
        self.watchers_lock = threading.Lock()

        # conn → {nick, room, uid, avatar, ip, mute, deaf, is_streaming, status_icon, status_text}
        self.clients = {}

        # uid → addr  (UDP-адрес клиента)
        self.udp_map = {}

        # uid → room  (кэш для O(1) поиска в UDP-маршрутизации)
        self.uid_to_room = {}

        # streamer_uid → {watcher_uid: {nick, avatar, uid}}
        self.watchers = {}

        self.stats = {"packets": 0, "bytes": 0}
        self.start_time = time.time()

        # --- Голосование «Пнуть» (Nudge) ---
        # Структура: { room_name → { target_uid → { voter_uid → vote_timestamp } } }
        # Записи живут NUDGE_COOLDOWN_SEC; голоса старше кулдауна не засчитываются.
        self.nudge_votes = {}
        self.nudge_lock  = threading.Lock()

        # --- ABR: фидбек битрейта от зрителей ---
        # streamer_uid → { viewer_uid → requested_bitrate }
        # При каждом обновлении вычисляем min и шлём стримеру adjust_bitrate
        # только если значение изменилось.
        self.abr_viewer_bitrates: dict[int, dict[int, int]] = {}
        self.abr_current:         dict[int, int]            = {}  # streamer_uid → текущий битрейт
        self.abr_lock = threading.Lock()

        # ABR гистерезис: метка времени последнего ПОВЫШЕНИЯ битрейта.
        # Понижение (при лагах) — немедленно.
        # Повышение (когда пинг улучшился) — не чаще чем раз в ABR_UPGRADE_COOLDOWN_SEC.
        # Без кулдауна небольшой джиттер RTT (50→55→50→55 мс) вызывал бы
        # непрерывные перезапуски энкодера каждые 4 секунды.
        self.abr_last_upgrade: dict[int, float] = {}   # streamer_uid → timestamp
        ABR_UPGRADE_COOLDOWN_SEC = 10.0                # константа прямо в __init__ (не в config)

        # -------------------------------------------------------------------
        # Upload-side ABR: сервер измеряет фактический входящий поток стримера.
        #
        # Проблема без этого механизма:
        #   Стример с плохим upload (напр. 1 Mbps) выставляет энкодер на 6 Mbps.
        #   Сервер получает кадры burst-ами с огромными паузами.
        #   Зрители видят битые/замороженные кадры даже при хорошем своём download.
        #   Viewer-driven ABR не поможет: он измеряет RTT зрителя, а не upload
        #   стримера. Сервер о проблеме ничего не знает.
        #
        # Решение:
        #   В udp_handler считаем байты от каждого стримера.
        #   _upload_abr_loop каждые 4 сек вычисляет фактический upload bitrate.
        #   Если actual < 70% от configured → сервер голосует в min() ABR системы
        #   с uid=0 (SERVER_VOTER), ограничивая стримера до реальной пропускной способности.
        # -------------------------------------------------------------------
        self._streamer_rx_bytes: dict[int, int]   = {}  # uid → байты за текущее окно
        self._streamer_rx_start: dict[int, float] = {}  # uid → начало текущего окна

        # -------------------------------------------------------------------
        # Simulcast: раздельная маршрутизация HQ/LQ потоков.
        #
        # Когда стример поддерживает simulcast (nvenc), он шлёт два потока:
        #   HQ (flags=FLAG_VIDEO)           → сильным зрителям (>= ABR_LQ_THRESHOLD)
        #   LQ (flags=FLAG_VIDEO|FLAG_VIDEO_LQ) → слабым зрителям (< ABR_LQ_THRESHOLD)
        #
        # _streamer_simulcast[uid] = True — как только пришёл первый LQ-пакет.
        # При simulcast _abr_update НЕ отправляет adjust_bitrate стримеру:
        # энкодеры фиксированы (HQ=6Mbps, LQ=800kbps), спираль ABR невозможна.
        # -------------------------------------------------------------------
        self._streamer_simulcast: dict[int, bool]  = {}  # uid → simulcast активен

    # ------------------------------------------------------------------
    # Мониторинг
    # ------------------------------------------------------------------
    def stats_monitor(self):
        last_bytes = 0
        while True:
            time.sleep(5)
            with self.clients_lock:
                active = len(self.clients)
            curr_bytes = self.stats["bytes"]          # int — атомарное чтение
            diff = (curr_bytes - last_bytes) / 1024 / 5
            print(f"[Stats] Active: {active} | Traffic: {diff:.1f} KB/s")
            last_bytes = curr_bytes

    # ------------------------------------------------------------------
    # UDP-маршрутизация
    # ------------------------------------------------------------------
    def udp_handler(self):
        """
        FIX #1 + FIX #2: UDP-поток держит лок только на минимальное время —
        ровно столько, сколько нужно для чтения адресов из словаря.
        Все sendto() выполняются УЖЕ после освобождения лока.

        Было (псевдокод):
            with self.lock:          # захват
                for addr in targets:
                    sendto(addr)     # I/O внутри лока → всё остальное стоит

        Стало:
            with self.udp_lock:
                targets = [...]      # только копирование адресов — микросекунды
            for addr in targets:
                sendto(addr)         # I/O вне лока
        """
        while True:
            try:
                data, addr = self.udp_sock.recvfrom(BUFFER_SIZE)
                if len(data) < UDP_HEADER_SIZE:
                    continue

                sender_uid, msg_ts, seq, flags = UDP_HEADER_STRUCT.unpack(data[:UDP_HEADER_SIZE])

                # Ping: отвечаем немедленно, без локов
                if flags == 254:
                    self.udp_sock.sendto(data, addr)
                    continue

                # Обновляем UDP-адрес отправителя.
                # stats обновляем ВНЕ лока — простые инты, GIL достаточен.
                # Раньше они были внутри udp_lock без необходимости: чтение из
                # stats_monitor() никогда не держало udp_lock, а запись int — атомарна.
                with self.udp_lock:
                    self.udp_map[sender_uid] = addr
                    sender_room = self.uid_to_room.get(sender_uid)
                self.stats["packets"] += 1
                self.stats["bytes"] += len(data)

                if not sender_room:
                    continue

                is_video = bool(flags & FLAG_VIDEO)
                is_stream_audio = bool(flags & FLAG_STREAM_AUDIO)
                is_stream_voices = bool(flags & FLAG_STREAM_VOICES)
                is_whisper = bool(flags & FLAG_WHISPER)

                if is_whisper:
                    # ШЁПОТ → только target_uid.
                    # Payload: [target_uid: 4 байта big-endian unsigned int] + [opus].
                    # Сервер извлекает target_uid и доставляет пакет только ему.
                    # Остальные участники комнаты пакет не получают.
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

                elif is_video:
                    # ВИДЕО → зрители стримера.
                    # Upload-ABR: счётчик байт от стримера (без лока — udp_handler однопоточен).
                    self._streamer_rx_bytes[sender_uid] = (
                        self._streamer_rx_bytes.get(sender_uid, 0) + len(data)
                    )
                    if sender_uid not in self._streamer_rx_start:
                        self._streamer_rx_start[sender_uid] = time.time()

                    # Simulcast: определяем тип пакета (HQ или LQ).
                    is_lq = bool(flags & FLAG_VIDEO_LQ)

                    if is_lq and not self._streamer_simulcast.get(sender_uid, False):
                        # Первый LQ-пакет: переключаем стримера в simulcast-режим.
                        self._streamer_simulcast[sender_uid] = True
                        print(f"[Simulcast] streamer={sender_uid}: LQ-поток обнаружен, "
                              f"режим раздельной маршрутизации активирован")

                    if self._streamer_simulcast.get(sender_uid, False):
                        # Simulcast: HQ → сильным зрителям, LQ → слабым.
                        self._send_to_watchers_routed(sender_uid, data, is_lq)
                    else:
                        # Legacy: единый поток всем зрителям.
                        self._send_to_watchers(sender_uid, data)

                elif is_stream_audio and is_stream_voices:
                    # ГОЛОСОВОЙ ПОТОК СТРИМА → только зрители стримера.
                    #
                    # Mix Minus без DSP: payload содержит [speaker_uid (4 байта)] + [opus].
                    # Каждый зритель получает полный пакет, но на клиенте отбрасывает
                    # пакет если speaker_uid == my_uid — он не слышит свой собственный голос.
                    #
                    # Сервер не фильтрует по speaker_uid, т.к.:
                    # 1) Не знает my_uid каждого зрителя в момент маршрутизации без доп. лока.
                    # 2) Это O(1) на клиенте vs O(N зрителей) на сервере.
                    self._send_to_watchers(sender_uid, data)

                elif is_stream_audio:
                    # СТРИМ-АУДИО → только зрители стримера (аналогично видео).
                    # Зрители сами отфильтруют голоса своих собеседников на клиенте.
                    self._send_to_watchers(sender_uid, data)

                else:
                    # АУДИО → все в той же комнате, кроме отправителя.
                    # FIX: Убираем вложенный лок (clients_lock → udp_lock).
                    # Старый код захватывал udp_lock ВНУТРИ clients_lock →
                    # риск дедлока если другой поток держит udp_lock и ждёт clients_lock.
                    # Новый код:
                    #   1. Под clients_lock собираем список uid получателей (int-ы, не адреса).
                    #   2. Отпускаем clients_lock.
                    #   3. Под udp_lock однократно разрешаем uid → addr.
                    #   4. sendto() — вообще без локов.
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

                    # sendto — вне любых локов
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
        uid = secrets.randbelow(10**9) + 1  # криптографически уникальный, без коллизий
        client_ip = addr[0]
        buffer = ""
        # JSONDecoder создаём ОДИН РАЗ на соединение — он stateless.
        # Каждое сообщение json.JSONDecoder() в старом коде = лишняя аллокация.
        _decoder = json.JSONDecoder()
        try:
            while True:
                chunk_bytes = conn.recv(4096)  # TCP: JSON-команды редко превышают 1 КБ
                if not chunk_bytes:
                    break
                buffer += chunk_bytes.decode('utf-8', errors='ignore')

                while True:
                    try:
                        msg, idx = _decoder.raw_decode(buffer)
                        buffer = buffer[idx:].lstrip()
                        action = msg.get('action')

                        if action == CMD_LOGIN:
                            client_nick   = msg.get('nick', 'User')
                            client_avatar = msg.get('avatar', '1.svg')
                            with self.clients_lock:
                                self.clients[conn] = {
                                    'nick':         client_nick,
                                    'room':         'General',
                                    'uid':          uid,
                                    'avatar':       client_avatar,
                                    'ip':           client_ip,
                                    'status_icon':  '',   # имя SVG-файла из assets/status/ или ''
                                    'status_text':  '',   # подсказка ≤ 30 символов или ''
                                }
                            with self.udp_lock:
                                self.uid_to_room[uid] = 'General'
                            conn.sendall(
                                json.dumps({'action': 'login_success', 'uid': uid}).encode('utf-8')
                            )
                            with self.clients_lock:
                                remaining = len(self.clients)
                            print(
                                f"[Server] ✔ {client_nick} подключился "
                                f"(комната: General, IP: {client_ip}) | Онлайн: {remaining}"
                            )
                            self.send_global_state()

                        elif action == CMD_JOIN_ROOM:
                            new_room = msg.get('room', 'General')
                            with self.clients_lock:
                                if conn in self.clients:
                                    self.clients[conn]['room'] = new_room
                            with self.udp_lock:
                                self.uid_to_room[uid] = new_room
                            self.send_global_state()

                        elif action == 'update_user':
                            with self.clients_lock:
                                if conn in self.clients:
                                    self.clients[conn]['nick']   = msg.get('nick',   self.clients[conn]['nick'])
                                    self.clients[conn]['avatar'] = msg.get('avatar', self.clients[conn]['avatar'])
                            self.send_global_state()

                        elif action == 'update_status':
                            with self.clients_lock:
                                if conn in self.clients:
                                    self.clients[conn]['mute'] = msg.get('mute', False)
                                    self.clients[conn]['deaf'] = msg.get('deaf', False)
                            self.send_global_state()

                        elif action == CMD_UPDATE_PRESENCE:
                            # Пользователь изменил свой «статус дела» (иконка + текст).
                            # status_icon: имя SVG-файла из assets/status/ или '' (нет статуса).
                            # status_text: произвольная подпись ≤ 30 символов или ''.
                            # Сервер только хранит и ретранслирует — не валидирует содержимое.
                            icon = msg.get('status_icon', '')[:64]   # ограничение длины имени файла
                            text = msg.get('status_text', '')[:30]   # ≤ 30 символов согласно ТЗ
                            with self.clients_lock:
                                if conn in self.clients:
                                    self.clients[conn]['status_icon'] = icon
                                    self.clients[conn]['status_text'] = text
                            self.send_global_state()

                        elif action == CMD_STREAM_START:
                            with self.clients_lock:
                                if conn in self.clients:
                                    self.clients[conn]['is_streaming'] = True
                                    print(f"[Server] {self.clients[conn]['nick']} запустил стрим")
                            self.send_global_state()

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
                                # Очищаем ABR-состояние и simulcast-флаг стримера
                                with self.abr_lock:
                                    self.abr_viewer_bitrates.pop(stopped_uid, None)
                                    self.abr_current.pop(stopped_uid, None)
                                    self.abr_last_upgrade.pop(stopped_uid, None)
                                self._streamer_simulcast.pop(stopped_uid, None)
                            self.send_global_state()

                        elif action == 'stream_watch_start':
                            streamer_uid  = msg.get('streamer_uid')
                            streamer_conn = None
                            if streamer_uid is not None:
                                with self.clients_lock:
                                    if conn in self.clients:
                                        watcher = self.clients[conn]
                                        w_uid   = watcher['uid']
                                        # Ищем коннект стримера
                                        for c_conn, c_data in self.clients.items():
                                            if (c_data['uid'] == streamer_uid
                                                    and c_data.get('is_streaming')):
                                                streamer_conn = c_conn
                                                break
                                with self.watchers_lock:
                                    if streamer_uid not in self.watchers:
                                        self.watchers[streamer_uid] = {}
                                    self.watchers[streamer_uid][w_uid] = {
                                        'uid':    w_uid,
                                        'nick':   watcher['nick'],
                                        'avatar': watcher.get('avatar', '1.svg'),
                                    }
                                print(
                                    f"[Server] {watcher['nick']} "
                                    f"начал смотреть стрим {streamer_uid}"
                                )
                            # IDR-запрос вне всех локов
                            if streamer_conn:
                                try:
                                    streamer_conn.sendall(
                                        json.dumps({'action': 'request_keyframe'}).encode('utf-8')
                                    )
                                    print(f"[Server] IDR запрошен у стримера uid={streamer_uid}")
                                except Exception:
                                    pass
                            self.send_global_state()

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
                                    print(f"[Server] {nick} перестал смотреть стрим {streamer_uid}")
                            self.send_global_state()

                        elif action == CMD_BITRATE_FEEDBACK:
                            # Зритель сообщает свой запрошенный битрейт.
                            # Валидируем: streamer_uid — int, bitrate — один из ABR_TIERS.
                            streamer_uid_abr = msg.get('streamer_uid')
                            req_bitrate      = msg.get('bitrate')
                            valid_bitrates   = {t[1] for t in ABR_TIERS}
                            if (isinstance(streamer_uid_abr, int)
                                    and isinstance(req_bitrate, int)
                                    and req_bitrate in valid_bitrates):
                                # _abr_update вызываем вне любых локов tcp_handler'а
                                self._abr_update(streamer_uid_abr, uid, req_bitrate)

                        elif action == 'request_keyframe':
                            # Зритель детектировал потерю UDP-пакетов и запрашивает IDR.
                            # Сервер ретранслирует команду стримеру — тот форсирует I-кадр.
                            # Команда мелкая (~50 байт), отправляем вне всех основных локов.
                            streamer_uid_idr = msg.get('streamer_uid')
                            if isinstance(streamer_uid_idr, int):
                                streamer_conn_idr = None
                                with self.clients_lock:
                                    for c_conn, c_data in self.clients.items():
                                        if (c_data['uid'] == streamer_uid_idr
                                                and c_data.get('is_streaming')):
                                            streamer_conn_idr = c_conn
                                            break
                                if streamer_conn_idr:
                                    try:
                                        streamer_conn_idr.sendall(
                                            json.dumps({'action': 'request_keyframe'}).encode('utf-8')
                                        )
                                        print(
                                            f"[Server] IDR ретранслирован стримеру uid={streamer_uid_idr}"
                                            f" (запрос от uid={uid})"
                                        )
                                    except Exception:
                                        pass

                        elif action == CMD_SOUNDBOARD:
                            # Добавляем ник отправителя — клиент покажет «кто включил»
                            with self.clients_lock:
                                sender_nick = self.clients[conn]['nick'] if conn in self.clients else '?'
                                conns = list(self.clients.keys())
                            msg['from_nick'] = sender_nick
                            payload = json.dumps(msg).encode('utf-8')
                            # FIX #2: sendall вне clients_lock — не блокируем чтение других потоков
                            for c in conns:
                                try:
                                    c.sendall(payload)
                                except Exception:
                                    pass

                        elif action == CMD_NUDGE_VOTE:
                            # ── Голосование «Пнуть» ──────────────────────────────────────
                            # Порог срабатывания: все участники комнаты, кроме цели.
                            # Пример: 4 человека в комнате, 1 АФК — нужно 3 голоса.
                            # Кулдаун: один voter может добавить голос не чаще
                            # NUDGE_COOLDOWN_SEC за одну цель.
                            target_uid = msg.get('target_uid')
                            if not isinstance(target_uid, int):
                                continue

                            now  = time.time()
                            fire = False
                            t_conn          = None
                            broadcaster_conns = []
                            voter_nick  = '?'
                            target_nick = '?'
                            voter_uid_v = None
                            voter_room  = None

                            with self.clients_lock:
                                if conn not in self.clients:
                                    continue
                                voter_info  = self.clients[conn]
                                voter_uid_v = voter_info['uid']
                                voter_room  = voter_info['room']
                                voter_nick  = voter_info['nick']

                                # uid всех участников комнаты
                                room_uids = [
                                    c['uid'] for c in self.clients.values()
                                    if c['room'] == voter_room
                                ]
                                # порог = все в комнате, кроме цели
                                threshold = max(1, len(room_uids) - 1)

                                # Находим conn и ник цели
                                for c_conn, c_data in self.clients.items():
                                    if (c_data['uid'] == target_uid
                                            and c_data['room'] == voter_room):
                                        t_conn      = c_conn
                                        target_nick = c_data['nick']
                                        break

                                # broadcast-список — все участники комнаты
                                broadcaster_conns = [
                                    c_conn for c_conn, c_data in self.clients.items()
                                    if c_data['room'] == voter_room
                                ]

                            if t_conn is None:
                                # цель не в нашей комнате — игнорируем
                                continue

                            with self.nudge_lock:
                                room_votes   = self.nudge_votes.setdefault(voter_room, {})
                                target_votes = room_votes.setdefault(target_uid, {})

                                # Проверяем кулдаун для этого voter
                                last = target_votes.get(voter_uid_v, 0)
                                if now - last < NUDGE_COOLDOWN_SEC:
                                    remaining = int(NUDGE_COOLDOWN_SEC - (now - last))
                                    print(
                                        f"[Server] 👟 {voter_nick} → Пнуть {target_nick}"
                                        f" — кулдаун ещё {remaining} с"
                                    )
                                    continue

                                target_votes[voter_uid_v] = now

                                # Считаем только активные (не протухшие) голоса
                                active = sum(
                                    1 for uid_v, ts in target_votes.items()
                                    if now - ts < NUDGE_COOLDOWN_SEC
                                )
                                print(
                                    f"[Server] 👟 {voter_nick} → Пнуть {target_nick}"
                                    f" ({active}/{threshold} голосов)"
                                )

                                if active >= threshold:
                                    # Сбрасываем голоса — следующий пнёт снова через кулдаун
                                    room_votes.pop(target_uid, None)
                                    fire = True

                            if fire:
                                # Отправляем play_nudge только цели
                                try:
                                    t_conn.sendall(
                                        json.dumps({'action': CMD_PLAY_NUDGE}).encode('utf-8')
                                    )
                                    print(f"[Server] 👟 NUDGE FIRED → {target_nick}")
                                except Exception:
                                    pass

                                # Рассылаем nudge_triggered всем в комнате (тост у всех)
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
                    nick  = client_info.get('nick', 'Unknown')
                    room  = client_info.get('room', '?')
                    u_id  = client_info['uid']
                    remaining = len(self.clients)
                else:
                    remaining = len(self.clients)

            if u_id is not None:
                with self.udp_lock:
                    self.udp_map.pop(u_id, None)
                    self.uid_to_room.pop(u_id, None)
                with self.watchers_lock:
                    for s_uid in list(self.watchers.keys()):
                        self.watchers[s_uid].pop(u_id, None)
                    self.watchers.pop(u_id, None)
                # Убираем ABR-запросы и simulcast-флаг отключившегося пользователя
                with self.abr_lock:
                    for s_uid in list(self.abr_viewer_bitrates.keys()):
                        self.abr_viewer_bitrates[s_uid].pop(u_id, None)
                self._streamer_simulcast.pop(u_id, None)
                print(f"[Server] ✖ {nick} (комната: {room}) отключился | Онлайн: {remaining}")
            else:
                print(f"[Server] ✖ Незарегистрированный клиент {addr[0]} отключился")

            conn.close()
            self.send_global_state()

    # ------------------------------------------------------------------
    # ABR: обновить битрейт для стримера и уведомить его если изменился
    # ------------------------------------------------------------------
    def _abr_update(self, streamer_uid: int, viewer_uid: int, bitrate: int):
        """
        Обновляет запрос битрейта от viewer_uid для стримера streamer_uid.

        Simulcast-режим (nvenc):
            Сохраняем предпочтение зрителя для маршрутизации в udp_handler.
            Команду adjust_bitrate стримеру НЕ отправляем — энкодеры фиксированы
            (HQ=6Mbps, LQ=800kbps). Спираль ABR физически невозможна.

        Legacy-режим (libx264 / без simulcast):
            Вычисляем min по всем зрителям и шлём стримеру adjust_bitrate.
            Гистерезис: понижение немедленно, повышение с кулдауном 10 с.

        Вызывается из tcp_handler, вне каких-либо других локов.
        """
        ABR_UPGRADE_COOLDOWN_SEC = 10.0
        new_min = bitrate

        with self.abr_lock:
            if streamer_uid not in self.abr_viewer_bitrates:
                self.abr_viewer_bitrates[streamer_uid] = {}
            self.abr_viewer_bitrates[streamer_uid][viewer_uid] = bitrate

            # В simulcast-режиме abr_viewer_bitrates используется только
            # для маршрутизации в _send_to_watchers_routed — выходим сразу.
            if self._streamer_simulcast.get(streamer_uid, False):
                return

            # --- Legacy ABR: вычисляем min и решаем — менять ли битрейт ---
            new_min = min(self.abr_viewer_bitrates[streamer_uid].values())
            current = self.abr_current.get(streamer_uid)

            if current == new_min:
                return  # ничего не изменилось

            is_downgrade = (current is not None and new_min < current)

            if not is_downgrade:
                # Повышение — проверяем кулдаун
                now = time.time()
                last_upgrade = self.abr_last_upgrade.get(streamer_uid, 0.0)
                if now - last_upgrade < ABR_UPGRADE_COOLDOWN_SEC:
                    return  # слишком рано повышать
                self.abr_last_upgrade[streamer_uid] = now

            # FIX: сохраняем старый битрейт ДО перезаписи — нужен для стрелки в логе.
            # Старый код читал abr_current[streamer_uid] ПОСЛЕ new_min, поэтому
            # new_min < new_min всегда False и стрелка всегда показывала "↑".
            old_bitrate = current
            self.abr_current[streamer_uid] = new_min

        # Находим conn стримера вне abr_lock
        streamer_conn = None
        with self.clients_lock:
            for c_conn, c_data in self.clients.items():
                if c_data['uid'] == streamer_uid and c_data.get('is_streaming'):
                    streamer_conn = c_conn
                    break

        if streamer_conn is None:
            return

        try:
            streamer_conn.sendall(
                json.dumps({
                    'action':  CMD_ADJUST_BITRATE,
                    'bitrate': new_min,
                }).encode('utf-8')
            )
            kbps = new_min // 1000
            direction = "↓" if (old_bitrate is not None and new_min < old_bitrate) else "↑"
            print(f"[ABR] streamer={streamer_uid}: {direction} новый битрейт {kbps} kbps "
                  f"(viewer={viewer_uid} запросил {bitrate//1000} kbps)")
        except Exception as e:
            print(f"[ABR] Ошибка отправки adjust_bitrate стримеру {streamer_uid}: {e}")

    # ------------------------------------------------------------------
    # Вспомогательный метод: отправка пакета всем зрителям стримера
    # ------------------------------------------------------------------
    def _send_to_watchers(self, sender_uid: int, data: bytes):
        """
        Отправляет UDP-пакет всем зрителям стримера sender_uid.

        Порядок локов намеренно фиксирован: watchers_lock → udp_lock.
        sendto() выполняется вне любых локов.
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
    # Вспомогательный метод: маршрутизация HQ/LQ при Simulcast
    # ------------------------------------------------------------------
    def _send_to_watchers_routed(self, sender_uid: int, data: bytes, is_lq: bool):
        """
        Simulcast-маршрутизация: отправляет пакет только тем зрителям,
        чьё качественное предпочтение совпадает с типом пакета.

        is_lq=True  (FLAG_VIDEO_LQ): отправить зрителям с bitrate ≤ ABR_LQ_THRESHOLD
        is_lq=False (HQ):            отправить зрителям с bitrate >  ABR_LQ_THRESHOLD

        Зритель без записанного предпочтения (ещё не прислал feedback) → HQ по умолчанию.
        Это гарантирует, что новый зритель сразу видит максимальное качество, а при
        первом же bitrate_feedback попадает в правильный поток.

        Порядок локов фиксирован: watchers_lock → abr_lock → udp_lock.
        sendto() выполняется вне любых локов.
        """
        with self.watchers_lock:
            watcher_uids = list(self.watchers.get(sender_uid, {}).keys())

        if not watcher_uids:
            return

        with self.abr_lock:
            prefs = dict(self.abr_viewer_bitrates.get(sender_uid, {}))

        # Фильтруем: сопоставляем is_lq с предпочтением каждого зрителя
        target_uids = []
        for w_uid in watcher_uids:
            req_bps = prefs.get(w_uid, ABR_LQ_THRESHOLD + 1)  # default → HQ
            viewer_wants_lq = req_bps <= ABR_LQ_THRESHOLD
            if is_lq == viewer_wants_lq:
                target_uids.append(w_uid)

        if not target_uids:
            return

        with self.udp_lock:
            target_addrs = [
                self.udp_map[uid]
                for uid in target_uids
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
    def send_global_state(self):
        """
        FIX #2: sendall() выполняется вне clients_lock.
        FIX nested-lock: watchers_lock больше не захватывается ВНУТРИ clients_lock.

        Было:
            with clients_lock:
                with watchers_lock: ...   # риск дедлока при инверсии порядка

        Стало:
            1. Под watchers_lock берём полный снимок watchers.
            2. Под clients_lock строим payload, используя уже готовый снимок.
            3. sendall() без каких-либо локов.
        """
        # Шаг 1: снимок watchers под своим локом (без clients_lock)
        with self.watchers_lock:
            watchers_snapshot = {uid: dict(ws) for uid, ws in self.watchers.items()}

        # Шаг 2: собрать состояние и список получателей — быстро, под clients_lock
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
                    'watchers':     watchers_list,
                    'status_icon':  c.get('status_icon', ''),
                    'status_text':  c.get('status_text', ''),
                })
                conns_snapshot.append(c_conn)

        payload = json.dumps({'action': CMD_SYNC_USERS, 'all_users': state}).encode('utf-8')

        # Шаг 3: отправить — без лока, медленный клиент не тормозит UDP
        for c_conn in conns_snapshot:
            try:
                c_conn.sendall(payload)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Upload-side ABR: сервер измеряет входящий поток стримера
    # ------------------------------------------------------------------
    def _upload_abr_loop(self):
        """
        Каждые INTERVAL секунд вычисляет фактический upload bitrate каждого активного
        стримера на основе реально принятых UDP байт.

        Проблема, которую решает этот метод:
            Viewer-driven ABR слепой к upload стримера. Если у стримера нестабильный
            WiFi/мобильный интернет (средний upload 800 kbps, но энкодер пишет 6 Mbps),
            сервер получает пакеты бурстами. Зрители видят 300 мс тишины → лавину пакетов
            → переполнение receive-буфера → потери → IDR-шторм. ABR реагирует только
            через 4 сек через RTT зрителя — этого недостаточно.

        Решение:
            Сервер голосует в min() ABR системы как псевдо-зритель uid=0.
            Если actual_bitrate < THRESHOLD * configured → отправляем adjust_bitrate
            со значением actual (округлённым до ближайшего ABR тира).
            Если upload восстановился → снимаем ограничение (голос 6 Mbps).
            Используем существующий _abr_update() — никакой новой логики отправки.

        THRESHOLD 80%: небольшой запас на заголовки и jitter,
            не роняем битрейт из-за случайного кратковременного спада.
        """
        INTERVAL  = 4.0    # секунд — совпадает с ABR_FEEDBACK_INTERVAL_MS клиента
        THRESHOLD = 0.80   # если actual < 80% configured → ограничиваем
        SERVER_VOTER_UID = 0  # псевдо-viewer uid для голосования в min()

        # Отсортированные тиры по убыванию для поиска ближайшего подходящего
        sorted_tiers = sorted(ABR_TIERS, key=lambda x: x[1])  # по возрастанию bitrate

        while True:
            time.sleep(INTERVAL)

            # Снимаем счётчики атомарно: читаем + сбрасываем
            now = time.time()
            streamers = list(self._streamer_rx_start.keys())

            for uid in streamers:
                rx_bytes = self._streamer_rx_bytes.pop(uid, 0)
                t_start  = self._streamer_rx_start.pop(uid, now)
                elapsed  = now - t_start
                if elapsed < 0.5:
                    # Слишком короткое окно — пропускаем, данные недостоверны
                    continue

                actual_bps = int((rx_bytes * 8) / elapsed)

                # Узнаём текущий сконфигурированный битрейт стримера
                with self.abr_lock:
                    configured_bps = self.abr_current.get(uid)

                if configured_bps is None:
                    # Стример есть, но ABR ещё не устанавливал битрейт — пропускаем
                    continue

                if actual_bps >= int(configured_bps * THRESHOLD):
                    # Upload справляется — снимаем серверное ограничение
                    # (голосуем максимальным тиром, не мешаем viewer ABR)
                    max_bitrate = sorted_tiers[-1][1]
                    self._abr_update(uid, SERVER_VOTER_UID, max_bitrate)
                    continue

                # Upload недостаточен — находим подходящий тир
                # Берём тир чуть выше фактического (даём 10% запас для jitter)
                target_bps = int(actual_bps * 1.10)
                voted_bps  = sorted_tiers[0][1]  # fallback: минимальный тир
                for tier_rtt, tier_bps in sorted_tiers:
                    if tier_bps <= target_bps:
                        voted_bps = tier_bps
                    else:
                        break

                actual_kbps     = actual_bps // 1000
                configured_kbps = configured_bps // 1000
                voted_kbps      = voted_bps // 1000
                print(
                    f"[UploadABR] streamer={uid}: фактический upload {actual_kbps} kbps "
                    f"< порог {int(configured_kbps * THRESHOLD)} kbps "
                    f"(настроен {configured_kbps} kbps) → голосуем {voted_kbps} kbps"
                )
                self._abr_update(uid, SERVER_VOTER_UID, voted_bps)

    # ------------------------------------------------------------------
    # Запуск сервера
    # ------------------------------------------------------------------
    def start(self):
        threading.Thread(target=self.udp_handler,      daemon=True).start()
        threading.Thread(target=self.stats_monitor,    daemon=True).start()
        threading.Thread(target=self._upload_abr_loop, daemon=True).start()
        print(f"Server started. TCP:{DEFAULT_PORT_TCP}, UDP:{DEFAULT_PORT_UDP}")
        while True:
            conn, addr = self.tcp_sock.accept()
            # Отключаем алгоритм Нейгла на каждом принятом соединении.
            # Команды adjust_bitrate и request_keyframe — мелкие JSON (~50 байт).
            # Без TCP_NODELAY Нейгл буферизует их до 200 мс в ожидании полного сегмента.
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=self.tcp_handler, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    SFUServer().start()