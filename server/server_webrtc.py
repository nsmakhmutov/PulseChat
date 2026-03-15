import asyncio
import json
import threading

from config import (
    CMD_WEBRTC_OFFER, CMD_WEBRTC_ANSWER, CMD_WEBRTC_ICE,
    WEBRTC_ICE_TIMEOUT,
)

# ── Опциональный импорт aiortc ────────────────────────────────────────────────
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

    # ------------------------------------------------------------------
    # Остановка SFU (при закрытии сервера)
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """
        Полная остановка SFU: закрывает все RTCPeerConnection и event loop.

        Вызывается из SFUServer.stop_gracefully() / stop_silent() при
        завершении работы встроенного сервера.

        Порядок:
          1. Запускаем coroutine _close_all() — закрывает все PC (стримеры + зрители).
          2. Ждём завершения (max 1 сек) чтобы aiortc успел отправить BYE.
          3. Останавливаем asyncio loop → run_forever() завершается → поток выходит.
        """
        if self._loop is None or self._loop.is_closed():
            return

        async def _close_all():
            for uid in list(self._streamer_entries.keys()):
                await self.close_streamer(uid)
            for uid in list(self._viewer_entries.keys()):
                await self.close_viewer(uid)
            # Очищаем pending буферы
            self._pending_viewers.clear()
            self._pending_ice.clear()

        try:
            fut = asyncio.run_coroutine_threadsafe(_close_all(), self._loop)
            fut.result(timeout=1.0)
        except Exception as e:
            print(f"[SFU] shutdown close_all error: {e}")

        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass
        print("[SFU] shutdown: event loop остановлен")