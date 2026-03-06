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
        """
        payload = json.dumps(msg).encode('utf-8')
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, conn.sendall, payload)
        except Exception as e:
            print(f"[SFU] send_async error: {e}")

    @staticmethod
    async def _wait_ice_gathering(pc, timeout: float = WEBRTC_ICE_TIMEOUT) -> None:
        """
        Ждёт завершения ICE gathering с таймаутом.
        На RadminVPN (host-only ICE) gathering завершается за ~50–200 мс.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while pc.iceGatheringState != "complete":
            if asyncio.get_event_loop().time() >= deadline:
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
            # Обрабатываем ожидавших зрителей (подключились до стримера)
            # Запускаем через ensure_future — не блокируем on_track callback
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
                'action':      CMD_WEBRTC_OFFER,
                'role':        'viewer',
                'streamer_uid': streamer_uid,
                'sdp':         pc.localDescription.sdp,
                'type':        pc.localDescription.type,
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
    def __init__(self, host='0.0.0.0'):
        # --- TCP ---
        self.tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp_sock.bind((host, DEFAULT_PORT_TCP))
        self.tcp_sock.listen()

        # --- UDP (только голос комнаты + ping) ---
        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # SO_RCVBUF 8MB: голосовые пакеты не дропаются пока handler занят.
        # SO_SNDBUF 8MB: исходящая очередь не блокирует recv-путь.
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
                            new_room = msg.get('room', 'General')
                            with self.clients_lock:
                                if conn in self.clients:
                                    self.clients[conn]['room'] = new_room
                            with self.udp_lock:
                                self.uid_to_room[uid] = new_room
                            self.send_global_state()

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
                                        watcher      = self.clients[conn]
                                        w_uid        = watcher['uid']
                                        watcher_nick = watcher['nick']
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

                # Убираем пользователя из списков зрителей всех стримеров
                affected_streamers = []
                with self.watchers_lock:
                    for s_uid in list(self.watchers.keys()):
                        if u_id in self.watchers[s_uid]:
                            self.watchers[s_uid].pop(u_id, None)
                            affected_streamers.append(s_uid)
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
            {'action': CMD_SYNC_USERS, 'all_users': state}
        ).encode('utf-8')

        for c_conn in conns_snapshot:
            try:
                c_conn.sendall(payload)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Запуск сервера
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


if __name__ == "__main__":
    SFUServer().start()