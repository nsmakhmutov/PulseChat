# server.py — SFU сервер InPulse (UDP голос + TCP команды + WebRTC видео)
#
# ─── Архитектура после рефакторинга ────────────────────────────────────────────
#
#   SFUServer   — существующий синхронный сервер.
#                 TCP: команды, чат, файлы, presence, nudge, soundboard.
#                 UDP: голос комнаты, whisper, ping/keepalive.  ← БЕЗ ИЗМЕНЕНИЙ
#
#   WebRTCSFU   — НОВЫЙ asyncio-класс в отдельном daemon-потоке.
#                 Принимает WebRTC-offer от стримера, сохраняет треки через MediaRelay.
#                 Создаёт WebRTC-offer зрителю, форвардит ему треки стримера.
#                 Сигнализация: 3 новые TCP-команды поверх существующего JSON-протокола.
#
# ─── Что УДАЛЕНО по сравнению с предыдущей версией ────────────────────────────
#
#   UDP-видео маршрутизация     (is_video/is_stream_audio ветки в udp_handler)
#   Simulcast HQ/LQ             (_streamer_simulcast, _lq_needed_state, _send_to_watchers_routed)
#   Viewer-side ABR             (abr_viewer_bitrates, abr_current, abr_last_upgrade, _abr_update)
#   Upload ABR                  (_streamer_rx_bytes, _streamer_rx_start, _upload_abr_loop)
#   NACK relay                  (CMD_NACK → CMD_NACK_RELAY → стример)
#   IDR relay                   (request_keyframe TCP relay — WebRTC PLI берёт на себя)
#   CMD_BITRATE_FEEDBACK        (зритель больше не измеряет RTT вручную)
#   CMD_ADJUST_BITRATE          (WebRTC управляет через TWCC)
#   CMD_LQ_NEEDED               (simulcast на WebRTC SFU-уровне в будущем)
#
# ─── Что СОХРАНЕНО без изменений ───────────────────────────────────────────────
#
#   UDP: голос комнаты, whisper, ping/keepalive
#   TCP: login, join_room, update_user, update_status, presence
#   TCP: stream_start/stop, stream_watch_start/stop, soundboard, nudge, file_offer
#   Все локи и их дисциплина (clients_lock, udp_lock, watchers_lock, nudge_lock)
#   send_global_state(), _send_to_watchers(), stats_monitor()
#
# ─── ИСПРАВЛЕНИЯ (v2) ──────────────────────────────────────────────────────────
#
#   [FIX-4] asyncio.get_event_loop() заменён на asyncio.get_running_loop() во всех
#           async-методах WebRTCSFU (_send_async, _wait_ice_gathering).
#           asyncio.get_event_loop() устарел в Python 3.10 и вызывает
#           DeprecationWarning в уже запущенном loop; get_running_loop() — правильный
#           способ получить текущий loop из coroutine/async-контекста.
#
# ───────────────────────────────────────────────────────────────────────────────

import asyncio
import json
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
    WEBRTC_ICE_TIMEOUT,
    CMD_SERVER_TRANSFER, CMD_SERVER_MIGRATE,
    CMD_QUICK_MSG, QUICK_MSG_MAX_LEN,
    CMD_CREATE_CHANNEL, CMD_CHANNEL_CREATED, CMD_CHANNEL_DELETED,
    CMD_JOIN_CHANNEL_AUTH, CHANNEL_NAME_MAX_LEN, CHANNEL_PASS_MAX_LEN,
    SERVER_NAME_DEFAULT,
)

# ── Опциональный импорт aiortc (только для WebRTCSFU) ────────────────────────
# При отсутствии aiortc — сервер работает без WebRTC (голос/чат работают).
try:
    from aiortc import (
        RTCPeerConnection, RTCSessionDescription,
        RTCConfiguration,
    )
    from aiortc.contrib.media import MediaRelay
    AIORTC_AVAILABLE = True
except ImportError:
    AIORTC_AVAILABLE = False
    print("[Server] WARNING: aiortc не установлен — WebRTC видео недоступно")


# =============================================================================
# WebRTCSFU — asyncio SFU для видео/аудио стримов
# =============================================================================

class WebRTCSFU:
    """
    asyncio-based SFU (Selective Forwarding Unit) для WebRTC видео/аудио.

    Запускается в отдельном daemon-потоке с собственным asyncio event loop.
    tcp_handler вызывает методы через asyncio.run_coroutine_threadsafe().

    ─── Жизненный цикл стримера ────────────────────────────────────────────
    1. tcp_handler получает CMD_WEBRTC_OFFER (role="streamer") → вызывает
       handle_streamer_offer(uid, sdp, conn)
    2. SFU создаёт RTCPeerConnection, принимает video/audio треки,
       оборачивает их в MediaRelay
    3. Ждёт завершения ICE-gathering (host-only на RadminVPN = ~50 мс)
    4. Отправляет CMD_WEBRTC_ANSWER стримеру через conn.sendall()

    ─── Жизненный цикл зрителя ─────────────────────────────────────────────
    1. tcp_handler получает stream_watch_start → регистрирует в watchers,
       вызывает handle_viewer_connect(viewer_uid, streamer_uid, viewer_conn)
    2. SFU создаёт RTCPeerConnection для зрителя, добавляет реле-треки стримера
    3. Создаёт offer, ждёт ICE-gathering, отправляет CMD_WEBRTC_OFFER зрителю
    4. Зритель отвечает CMD_WEBRTC_ANSWER → handle_viewer_answer(uid, sdp)

    ─── ICE стратегия ──────────────────────────────────────────────────────
    В RadminVPN все клиенты в одной виртуальной сети (26.x.x.x).
    Host ICE кандидаты достаточны — STUN не нужен.
    Используем "gather-and-send" вместо trickle ICE для простоты:
    ждём завершения gathering, затем отправляем SDP со всеми кандидатами.
    CMD_WEBRTC_ICE поддерживается для будущей совместимости с STUN.

    ─── MediaRelay ──────────────────────────────────────────────────────────
    Треки от одного RTCPeerConnection нельзя напрямую добавить в другой.
    MediaRelay создаёт прокси-треки с общим буфером — один входящий трек
    может быть подписан несколькими зрителями без копирования данных.
    """

    def __init__(self):
        self._loop   = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="webrtc-sfu"
        )
        self._thread.start()

        # streamer_uid → {'pc': RTCPeerConnection, 'relay': MediaRelay,
        #                  'tracks': {kind: relayed_track}, 'conn': conn}
        self._streamer_entries: dict = {}

        # viewer_uid → {'pc': RTCPeerConnection, 'conn': conn,
        #                'streamer_uid': int}
        self._viewer_entries: dict = {}

        # Буфер ожидающих зрителей: streamer_uid → [(viewer_uid, conn)]
        # Используется если зритель подключился до завершения offer стримера.
        self._pending_viewers: dict[int, list] = {}

        # Входящие ICE-кандидаты до момента создания PC:
        # uid → [candidate_dict]
        self._pending_ice: dict[int, list] = {}

    # ------------------------------------------------------------------
    # Запуск asyncio loop
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def call_async(self, coro):
        """
        Отправляет корутину в asyncio loop из threading-контекста (tcp_handler).
        Возвращает concurrent.futures.Future — можно игнорировать для fire-and-forget.
        """
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    async def _send_async(self, conn, msg: dict) -> None:
        """
        Отправляет JSON-пакет клиенту из asyncio-контекста.
        run_in_executor: conn.sendall() блокирующий → не блокируем asyncio loop.

        [FIX-4] asyncio.get_event_loop() → asyncio.get_running_loop().
        get_running_loop() — правильный способ получить loop из coroutine.
        get_event_loop() устарел в Python 3.10+ в уже запущенном loop.
        """
        payload = json.dumps(msg).encode('utf-8')
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, conn.sendall, payload)
        except Exception as e:
            print(f"[SFU] send_async error: {e}")

    @staticmethod
    async def _wait_ice_gathering(pc, timeout: float = WEBRTC_ICE_TIMEOUT) -> None:
        """
        Ждёт завершения ICE gathering с таймаутом.
        На RadminVPN (host-only ICE) gathering завершается за ~50–200 мс.

        [FIX-4] asyncio.get_event_loop() → asyncio.get_running_loop().
        Оба вызова заменены — метод вызывается только из async-контекста.
        """
        loop     = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while pc.iceGatheringState != "complete":
            if loop.time() >= deadline:
                print(f"[SFU] ICE gathering timeout ({timeout}s) — отправляем что есть")
                break
            await asyncio.sleep(0.05)

    # ------------------------------------------------------------------
    # Стример: обработка offer
    # ------------------------------------------------------------------

    async def handle_streamer_offer(
        self, streamer_uid: int, sdp: str, sdp_type: str, conn
    ) -> None:
        """
        Принимает WebRTC offer от стримера.

        1. Закрывает старый PC стримера (переподключение).
        2. Создаёт новый RTCPeerConnection.
        3. on("track") → оборачивает трек в MediaRelay, сохраняет.
        4. setRemoteDescription(offer) → createAnswer → setLocalDescription.
        5. Ждёт завершения ICE gathering.
        6. Отправляет answer стримеру.
        7. Обрабатывает ожидающих зрителей.
        """
        # Закрываем предыдущую сессию если была
        old = self._streamer_entries.pop(streamer_uid, None)
        if old:
            try:
                await old['pc'].close()
            except Exception:
                pass

        cfg = RTCConfiguration(iceServers=[])
        pc  = RTCPeerConnection(cfg)

        relay = MediaRelay()
        entry = {
            'pc':     pc,
            'relay':  relay,
            'tracks': {},
            'conn':   conn,
        }
        self._streamer_entries[streamer_uid] = entry

        @pc.on("track")
        def on_track(track):
            # MediaRelay.subscribe(buffered=False): нет буферизации → минимальная задержка.
            # Несколько зрителей подпишутся на один и тот же физический трек.
            relayed = relay.subscribe(track, buffered=False)
            entry['tracks'][track.kind] = relayed
            print(
                f"[SFU] Стример uid={streamer_uid}: трек получен kind={track.kind}"
            )
            # Обрабатываем ожидавших зрителей (подключились до стримера).
            # ensure_future — не блокируем on_track callback.
            pending = self._pending_viewers.pop(streamer_uid, [])
            for v_uid, v_conn in pending:
                asyncio.ensure_future(
                    self.handle_viewer_connect(v_uid, streamer_uid, v_conn)
                )

        @pc.on("icecandidate")
        def on_ice(candidate):
            # Trickle ICE (для будущей поддержки STUN).
            # При host-only ICE кандидаты уже в SDP после gathering → эта ветка редка.
            if candidate:
                asyncio.ensure_future(self._send_async(conn, {
                    'action':     CMD_WEBRTC_ICE,
                    'target_uid': streamer_uid,
                    'candidate': {
                        'sdpMid':        candidate.sdpMid,
                        'sdpMLineIndex': candidate.sdpMLineIndex,
                        'candidate':     candidate.candidate,
                    },
                }))

        @pc.on("connectionstatechange")
        async def on_state():
            state = pc.connectionState
            print(f"[SFU] Стример uid={streamer_uid}: PC state → {state}")
            if state in ("failed", "closed", "disconnected"):
                await self.close_streamer(streamer_uid)

        # Принимаем offer, создаём answer
        try:
            await pc.setRemoteDescription(
                RTCSessionDescription(sdp=sdp, type=sdp_type)
            )
            # Добавляем буферизованные ICE-кандидаты (если клиент использует trickle)
            for ice in self._pending_ice.pop(streamer_uid, []):
                try:
                    from aiortc import RTCIceCandidate
                    cand = RTCIceCandidate(
                        sdpMid=ice['sdpMid'],
                        sdpMLineIndex=ice['sdpMLineIndex'],
                        candidate=ice['candidate'],
                    )
                    await pc.addIceCandidate(cand)
                except Exception as e:
                    print(f"[SFU] addIceCandidate error: {e}")

            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)

            # Ждём завершения ICE gathering (host-only = быстро)
            await self._wait_ice_gathering(pc)

            await self._send_async(conn, {
                'action': CMD_WEBRTC_ANSWER,
                'sdp':    pc.localDescription.sdp,
                'type':   pc.localDescription.type,
            })
            print(f"[SFU] Answer отправлен стримеру uid={streamer_uid}")

        except Exception as e:
            print(f"[SFU] handle_streamer_offer error uid={streamer_uid}: {e}")
            self._streamer_entries.pop(streamer_uid, None)

    # ------------------------------------------------------------------
    # Зритель: создание offer и подключение
    # ------------------------------------------------------------------

    async def handle_viewer_connect(
        self, viewer_uid: int, streamer_uid: int, viewer_conn
    ) -> None:
        """
        Создаёт WebRTC соединение для нового зрителя.

        Если треки стримера ещё не готовы — кладём зрителя в pending_viewers.
        Как только on("track") от стримера сработает — pending обработается автоматически.

        Порядок:
        1. Проверяем наличие relay-треков стримера.
        2. Создаём RTCPeerConnection для зрителя.
        3. Добавляем relay-треки: viewer_pc.addTrack(relayed_track).
        4. on("track") → video_engine.add_receiver() (на клиентской стороне).
        5. createOffer → setLocalDescription → wait ICE → send offer зрителю.
        """
        entry = self._streamer_entries.get(streamer_uid)
        if entry is None or not entry['tracks']:
            # Стример ещё не прислал offer или треки не готовы
            self._pending_viewers.setdefault(streamer_uid, []).append(
                (viewer_uid, viewer_conn)
            )
            print(
                f"[SFU] Зритель uid={viewer_uid}: стример uid={streamer_uid} "
                f"ещё не готов — помещён в pending"
            )
            return

        # Закрываем предыдущий PC зрителя (переподключение)
        old = self._viewer_entries.pop(viewer_uid, None)
        if old:
            try:
                await old['pc'].close()
            except Exception:
                pass

        cfg = RTCConfiguration(iceServers=[])
        pc  = RTCPeerConnection(cfg)

        # Добавляем relay-треки стримера
        for kind, relayed_track in entry['tracks'].items():
            pc.addTrack(relayed_track)

        self._viewer_entries[viewer_uid] = {
            'pc':           pc,
            'conn':         viewer_conn,
            'streamer_uid': streamer_uid,
        }

        @pc.on("icecandidate")
        def on_ice(candidate):
            if candidate:
                asyncio.ensure_future(self._send_async(viewer_conn, {
                    'action':     CMD_WEBRTC_ICE,
                    'target_uid': viewer_uid,
                    'candidate': {
                        'sdpMid':        candidate.sdpMid,
                        'sdpMLineIndex': candidate.sdpMLineIndex,
                        'candidate':     candidate.candidate,
                    },
                }))

        @pc.on("connectionstatechange")
        async def on_state():
            state = pc.connectionState
            print(f"[SFU] Зритель uid={viewer_uid}: PC state → {state}")
            if state in ("failed", "closed", "disconnected"):
                await self.close_viewer(viewer_uid)

        try:
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            await self._wait_ice_gathering(pc)

            await self._send_async(viewer_conn, {
                'action':       CMD_WEBRTC_OFFER,
                'role':         'viewer',
                'streamer_uid': streamer_uid,
                'sdp':          pc.localDescription.sdp,
                'type':         pc.localDescription.type,
            })
            print(
                f"[SFU] Offer отправлен зрителю uid={viewer_uid} "
                f"(стример uid={streamer_uid})"
            )
        except Exception as e:
            print(f"[SFU] handle_viewer_connect error uid={viewer_uid}: {e}")
            self._viewer_entries.pop(viewer_uid, None)

    # ------------------------------------------------------------------
    # Зритель: обработка answer
    # ------------------------------------------------------------------

    async def handle_viewer_answer(
        self, viewer_uid: int, sdp: str, sdp_type: str
    ) -> None:
        """
        Принимает WebRTC answer от зрителя.
        Завершает ICE negotiation на стороне сервера.
        """
        entry = self._viewer_entries.get(viewer_uid)
        if entry is None:
            print(f"[SFU] handle_viewer_answer: нет PC для uid={viewer_uid}")
            return
        try:
            await entry['pc'].setRemoteDescription(
                RTCSessionDescription(sdp=sdp, type=sdp_type)
            )
            # Добавляем буферизованные ICE-кандидаты
            for ice in self._pending_ice.pop(viewer_uid, []):
                try:
                    from aiortc import RTCIceCandidate
                    cand = RTCIceCandidate(
                        sdpMid=ice['sdpMid'],
                        sdpMLineIndex=ice['sdpMLineIndex'],
                        candidate=ice['candidate'],
                    )
                    await entry['pc'].addIceCandidate(cand)
                except Exception:
                    pass
            print(f"[SFU] Answer принят от зрителя uid={viewer_uid}")
        except Exception as e:
            print(f"[SFU] handle_viewer_answer error uid={viewer_uid}: {e}")

    # ------------------------------------------------------------------
    # ICE кандидаты (trickle ICE)
    # ------------------------------------------------------------------

    async def handle_ice_candidate(
        self, uid: int, candidate_dict: dict
    ) -> None:
        """
        Добавляет ICE-кандидат к нужному PC.
        Буферизует если PC ещё не создан (гонка: ICE пришёл до offer/answer).
        """
        pc = None
        if uid in self._streamer_entries:
            pc = self._streamer_entries[uid]['pc']
        elif uid in self._viewer_entries:
            pc = self._viewer_entries[uid]['pc']

        if pc is None:
            # PC ещё не создан — буферизуем
            self._pending_ice.setdefault(uid, []).append(candidate_dict)
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
            print(f"[SFU] handle_ice_candidate uid={uid}: {e}")

    # ------------------------------------------------------------------
    # Закрытие соединений
    # ------------------------------------------------------------------

    async def close_streamer(self, streamer_uid: int) -> None:
        """
        Закрывает PC стримера и все PC его зрителей.
        Вызывается при CMD_STREAM_STOP или потере соединения.
        """
        entry = self._streamer_entries.pop(streamer_uid, None)
        if entry:
            try:
                await entry['pc'].close()
            except Exception:
                pass
            print(f"[SFU] Стример uid={streamer_uid}: PC закрыт")

        # Закрываем всех зрителей этого стримера
        victims = [
            v_uid for v_uid, ve in list(self._viewer_entries.items())
            if ve['streamer_uid'] == streamer_uid
        ]
        for v_uid in victims:
            await self.close_viewer(v_uid)

        self._pending_viewers.pop(streamer_uid, None)

    async def close_viewer(self, viewer_uid: int) -> None:
        """
        Закрывает PC зрителя.
        Вызывается при stream_watch_stop или потере соединения.
        """
        entry = self._viewer_entries.pop(viewer_uid, None)
        if entry:
            try:
                await entry['pc'].close()
            except Exception:
                pass
            print(f"[SFU] Зритель uid={viewer_uid}: PC закрыт")


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
        self.sfu: WebRTCSFU | None = None

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
        self._channels: dict = {
            'General': {'password': None, 'permanent': True},
        }
        self._channels_lock = threading.Lock()

        # Кэш авторизованных клиентов для защищённых каналов
        # conn → set[channel_name]
        self._channel_auth: dict = {}
        self._channel_auth_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Управление каналами
    # ------------------------------------------------------------------
    def get_client_count(self) -> int:
        """Возвращает текущее число подключённых клиентов. Используется ServerAnnouncer."""
        with self.clients_lock:
            return len(self.clients)

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
                            with self.clients_lock:
                                self.clients[conn] = {
                                    'nick':         client_nick,
                                    'room':         'General',
                                    'uid':          uid,
                                    'avatar':       client_avatar,
                                    'ip':           client_ip,
                                    'status_icon':  '',
                                    'status_text':  '',
                                }
                            with self.udp_lock:
                                self.uid_to_room[uid] = 'General'
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
                                f"(General, IP: {client_ip}) | Онлайн: {remaining}"
                            )
                            self.send_global_state()

                        # ── Join Room ─────────────────────────────────────────
                        elif action == CMD_JOIN_ROOM:
                            new_room = msg.get('room', 'General')[:CHANNEL_NAME_MAX_LEN]

                            # Проверяем существование и пароль канала
                            with self._channels_lock:
                                ch = self._channels.get(new_room)

                            if new_room != 'General' and ch is None:
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
                                    self.send_global_state()
                                else:
                                    conn.sendall(json.dumps({
                                        'action': 'create_channel_result',
                                        'ok': False, 'reason': 'already_exists',
                                    }).encode('utf-8'))

                        # ── Update User ───────────────────────────────────────
                        elif action == 'update_user':
                            with self.clients_lock:
                                if conn in self.clients:
                                    self.clients[conn]['nick']   = msg.get('nick',   self.clients[conn]['nick'])
                                    self.clients[conn]['avatar'] = msg.get('avatar', self.clients[conn]['avatar'])
                            self.send_global_state()

                        # ── Update Status ─────────────────────────────────────
                        elif action == 'update_status':
                            with self.clients_lock:
                                if conn in self.clients:
                                    self.clients[conn]['mute'] = msg.get('mute', False)
                                    self.clients[conn]['deaf'] = msg.get('deaf', False)
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
                            self.send_global_state()

                        # ── Stream Start ──────────────────────────────────────
                        elif action == CMD_STREAM_START:
                            with self.clients_lock:
                                if conn in self.clients:
                                    self.clients[conn]['is_streaming'] = True
                                    print(f"[Server] {self.clients[conn]['nick']} запустил стрим")
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
                                    self.sfu.call_async(
                                        self.sfu.close_streamer(stopped_uid)
                                    )
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
                                # WebRTC: создаём offer для зрителя через SFU
                                if self.sfu:
                                    self.sfu.call_async(
                                        self.sfu.handle_viewer_connect(
                                            w_uid, streamer_uid, conn
                                        )
                                    )
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
                                        self.sfu.call_async(
                                            self.sfu.close_viewer(w_uid)
                                        )
                            self.send_global_state()

                        # ── WebRTC Offer (от стримера или зрителя) ────────────
                        elif action == CMD_WEBRTC_OFFER:
                            sdp      = msg.get('sdp')
                            sdp_type = msg.get('type', 'offer')
                            role     = msg.get('role', '')

                            if not sdp or not self.sfu:
                                continue

                            if role == 'streamer':
                                # Стример прислал offer → SFU принимает треки
                                self.sfu.call_async(
                                    self.sfu.handle_streamer_offer(
                                        uid, sdp, sdp_type, conn
                                    )
                                )
                            # role == 'viewer' не обрабатывается здесь:
                            # offer от SFU к зрителю уже отправлен в handle_viewer_connect.
                            # Если придёт — это ошибка протокола, игнорируем.

                        # ── WebRTC Answer (от зрителя) ────────────────────────
                        elif action == CMD_WEBRTC_ANSWER:
                            sdp      = msg.get('sdp')
                            sdp_type = msg.get('type', 'answer')

                            if sdp and self.sfu:
                                # Зритель ответил на наш offer
                                self.sfu.call_async(
                                    self.sfu.handle_viewer_answer(uid, sdp, sdp_type)
                                )

                        # ── WebRTC ICE Candidate ──────────────────────────────
                        elif action == CMD_WEBRTC_ICE:
                            candidate = msg.get('candidate')
                            if candidate and self.sfu:
                                self.sfu.call_async(
                                    self.sfu.handle_ice_candidate(uid, candidate)
                                )

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
                                threshold = max(1, len(room_uids) - 1)

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

                    except json.JSONDecodeError:
                        break

        except Exception as e:
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
                    # Если был стримером — закрываем стример PC + все зрители
                    self.sfu.call_async(self.sfu.close_streamer(u_id))
                    # Если был зрителем — закрываем зритель PC
                    self.sfu.call_async(self.sfu.close_viewer(u_id))

                print(
                    f"[Server] ✖ {nick} (комната: {room}) "
                    f"отключился | Онлайн: {remaining}"
                )
            else:
                print(f"[Server] ✖ Незарегистрированный клиент {addr[0]} отключился")

            conn.close()
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
    def send_global_state(self):
        """
        FIX #2: sendall() выполняется вне clients_lock.
        FIX nested-lock: watchers_lock не захватывается внутри clients_lock.

        1. Под watchers_lock берём снимок watchers.
        2. Под clients_lock строим payload, используя снимок.
        3. sendall() без каких-либо локов.
        """
        with self.watchers_lock:
            watchers_snapshot = {uid: dict(ws) for uid, ws in self.watchers.items()}

        with self._host_order_lock:
            host_order_snapshot  = list(self._host_order)
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
                    'watchers':     watchers_list,
                    'status_icon':  c.get('status_icon', ''),
                    'status_text':  c.get('status_text', ''),
                })
                conns_snapshot.append(c_conn)

        payload = json.dumps(
            {'action': CMD_SYNC_USERS, 'all_users': state,
             'host_order': host_order_snapshot,
             'server_host_uid': server_host_uid,
             'channel_list': self._get_channel_list()}
        ).encode('utf-8')

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
        self._owner_ip    = host_ip   # FIX: сохраняем IP владельца для приоритета в host_order

        # WebRTCSFU
        if AIORTC_AVAILABLE:
            self.sfu = WebRTCSFU()
            print("[Server] WebRTCSFU запущен (встроенный режим)")
        else:
            print("[Server] WebRTCSFU ОТКЛЮЧЁН (aiortc не установлен)")

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
        # Запускаем WebRTCSFU если aiortc установлен
        if AIORTC_AVAILABLE:
            self.sfu = WebRTCSFU()
            print("[Server] WebRTCSFU запущен (aiortc доступен)")
        else:
            print("[Server] WebRTCSFU ОТКЛЮЧЁН (aiortc не установлен)")

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

    def __init__(self):
        self._server = None  # SFUServer | None

    @classmethod
    def get(cls) -> 'EmbeddedServerManager':
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