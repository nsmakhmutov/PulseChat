import socket
import threading
import json
import time
from config import (
    DEFAULT_PORT_TCP, DEFAULT_PORT_UDP, BUFFER_SIZE,
    UDP_RECV_BUFFER_SIZE, UDP_SEND_BUFFER_SIZE,
    UDP_HEADER_STRUCT, UDP_HEADER_SIZE, FLAG_VIDEO, FLAG_STREAM_AUDIO,
    CMD_LOGIN, CMD_JOIN_ROOM, CMD_STREAM_START, CMD_STREAM_STOP,
    CMD_SYNC_USERS, CMD_SOUNDBOARD, FLAG_LOOPBACK_AUDIO, FLAG_STREAM_VOICES,
    FLAG_WHISPER, STREAM_VOICE_HEADER_STRUCT, STREAM_VOICE_HEADER_SIZE
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

        # conn → {nick, room, uid, avatar, ip, mute, deaf, is_streaming}
        self.clients = {}

        # uid → addr  (UDP-адрес клиента)
        self.udp_map = {}

        # uid → room  (кэш для O(1) поиска в UDP-маршрутизации)
        self.uid_to_room = {}

        # streamer_uid → {watcher_uid: {nick, avatar, uid}}
        self.watchers = {}

        self.stats = {"packets": 0, "bytes": 0}
        self.start_time = time.time()

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

                # Обновляем UDP-адрес отправителя и статистику
                with self.udp_lock:
                    self.udp_map[sender_uid] = addr
                    # stats — простые инты, GIL достаточен
                    self.stats["packets"] += 1
                    self.stats["bytes"] += len(data)
                    sender_room = self.uid_to_room.get(sender_uid)

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
                    # ВИДЕО → только зрители стримера
                    # Копируем список адресов под коротким локом
                    with self.watchers_lock:
                        watcher_uids = list(self.watchers.get(sender_uid, {}).keys())

                    with self.udp_lock:
                        target_addrs = [
                            self.udp_map[uid]
                            for uid in watcher_uids
                            if uid in self.udp_map
                        ]

                    # sendto — вне любых локов
                    for target_addr in target_addrs:
                        try:
                            self.udp_sock.sendto(data, target_addr)
                        except Exception:
                            pass

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
                    with self.watchers_lock:
                        watcher_uids = list(self.watchers.get(sender_uid, {}).keys())

                    with self.udp_lock:
                        target_addrs = [
                            self.udp_map[uid]
                            for uid in watcher_uids
                            if uid in self.udp_map
                        ]

                    for target_addr in target_addrs:
                        try:
                            self.udp_sock.sendto(data, target_addr)
                        except Exception:
                            pass

                elif is_stream_audio:
                    # СТРИМ-АУДИО → только зрители стримера (аналогично видео)
                    # Зрители сами отфильтруют голоса своих собеседников на клиенте
                    with self.watchers_lock:
                        watcher_uids = list(self.watchers.get(sender_uid, {}).keys())

                    with self.udp_lock:
                        target_addrs = [
                            self.udp_map[uid]
                            for uid in watcher_uids
                            if uid in self.udp_map
                        ]

                        # --- ДОБАВЛЕННЫЙ ЛОГ ---
                    is_loopback = bool(flags & FLAG_LOOPBACK_AUDIO)
                    if seq % 50 == 0:
                        print(f"[Server-UDP] Роутинг стрим-аудио (loopback={is_loopback}) "
                              f"от {sender_uid} для {len(target_addrs)} зрителей")

                    for target_addr in target_addrs:
                        try:
                            self.udp_sock.sendto(data, target_addr)
                        except Exception:
                            pass

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
        uid = int(time.time() * 1000) % 1000000
        client_ip = addr[0]
        buffer = ""
        # JSONDecoder создаём ОДИН РАЗ на соединение — он stateless.
        # Каждое сообщение json.JSONDecoder() в старом коде = лишняя аллокация.
        _decoder = json.JSONDecoder()
        try:
            while True:
                chunk_bytes = conn.recv(BUFFER_SIZE)
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
                                    'nick':   client_nick,
                                    'room':   'General',
                                    'uid':    uid,
                                    'avatar': client_avatar,
                                    'ip':     client_ip,
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

                        elif action == CMD_SOUNDBOARD:
                            payload = json.dumps(msg).encode('utf-8')
                            # FIX #2: sendall вне clients_lock — не блокируем чтение других потоков
                            with self.clients_lock:
                                conns = list(self.clients.keys())
                            for c in conns:
                                try:
                                    c.sendall(payload)
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
                print(f"[Server] ✖ {nick} (комната: {room}) отключился | Онлайн: {remaining}")
            else:
                print(f"[Server] ✖ Незарегистрированный клиент {addr[0]} отключился")

            conn.close()
            self.send_global_state()

    # ------------------------------------------------------------------
    # Рассылка глобального состояния по TCP
    # ------------------------------------------------------------------
    def send_global_state(self):
        """
        FIX #2: sendall() выполняется вне clients_lock.

        Было:
            with self.lock:
                for conn in self.clients:
                    conn.sendall(payload)   # блокирует лок на время всех TCP-отправок

        Стало:
            1. Берём лок → быстро строим payload и список коннектов → отпускаем лок
            2. Отправляем sendall() без какого-либо лока

        Это гарантирует, что UDP-поток никогда не ждёт TCP I/O.
        """
        # Шаг 1: собрать состояние и список получателей — быстро, под локом
        with self.clients_lock:
            state = {}
            conns_snapshot = []
            for c_conn, c in self.clients.items():
                c_uid = c['uid']
                with self.watchers_lock:
                    watchers_list = list(self.watchers.get(c_uid, {}).values())
                state.setdefault(c['room'], []).append({
                    'nick':         c['nick'],
                    'uid':          c_uid,
                    'avatar':       c.get('avatar', '1.svg'),
                    'mute':         c.get('mute', False),
                    'deaf':         c.get('deaf', False),
                    'ip':           c.get('ip', ''),
                    'is_streaming': c.get('is_streaming', False),
                    'watchers':     watchers_list,
                })
                conns_snapshot.append(c_conn)

        payload = json.dumps({'action': CMD_SYNC_USERS, 'all_users': state}).encode('utf-8')

        # Шаг 2: отправить — без лока, медленный клиент не тормозит UDP
        for c_conn in conns_snapshot:
            try:
                c_conn.sendall(payload)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Запуск сервера
    # ------------------------------------------------------------------
    def start(self):
        threading.Thread(target=self.udp_handler,   daemon=True).start()
        threading.Thread(target=self.stats_monitor, daemon=True).start()
        print(f"Server started. TCP:{DEFAULT_PORT_TCP}, UDP:{DEFAULT_PORT_UDP}")
        while True:
            conn, addr = self.tcp_sock.accept()
            threading.Thread(target=self.tcp_handler, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    SFUServer().start()