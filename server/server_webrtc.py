import asyncio
import json
import threading

from config import (
    CMD_WEBRTC_OFFER, CMD_WEBRTC_ANSWER, CMD_WEBRTC_ICE,
    WEBRTC_ICE_TIMEOUT,
)

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
# WebRTCSFU — asyncio SFU с поддержкой simulcast (HQ + LQ)
# =============================================================================

class WebRTCSFU:
    """
    asyncio-based SFU (Selective Forwarding Unit) для WebRTC видео/аудио.

    ─── Simulcast (HQ + LQ) ────────────────────────────────────────────────
    Стример отправляет ДВА видеотрека в одном RTCPeerConnection:
        1-й video track = HQ (добавлен первым через pc.addTrack в network_engine)
        2-й video track = LQ (DXCamTrackLQ, добавлен вторым)

    SFU определяет порядок по счётчику on_track:
        source_tracks = {'video_hq': ..., 'video_lq': ..., 'audio': ...}

    Зритель выбирает качество в stream_watch_start:
        {'action': 'stream_watch_start', 'streamer_uid': X, 'quality': 'lq'}
    По умолчанию quality='hq'. Если LQ трек не пришёл от стримера —
    fallback на HQ для всех зрителей.

    ─── ICE стратегия ──────────────────────────────────────────────────────
    Host-only ICE (RadminVPN 26.x.x.x). STUN не нужен.
    Gather-and-send: ждём завершения gathering, отправляем SDP целиком.
    """

    def __init__(self):
        self._loop   = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="webrtc-sfu"
        )
        self._thread.start()

        # streamer_uid → {
        #   'pc':           RTCPeerConnection,
        #   'relay':        MediaRelay,
        #   'tracks':       {key: relayed_track},     # для совместимости
        #   'source_tracks':{key: original_track},    # video_hq/video_lq/audio
        #   'conn':         socket,
        # }
        self._streamer_entries: dict = {}

        # viewer_uid → {'pc': RTCPeerConnection, 'conn': conn, 'streamer_uid': int}
        self._viewer_entries: dict = {}

        # Буфер ожидающих зрителей: streamer_uid → [(viewer_uid, conn, quality)]
        self._pending_viewers: dict[int, list] = {}

        # Входящие ICE-кандидаты до создания PC
        self._pending_ice: dict[int, list] = {}

    # ------------------------------------------------------------------
    # Запуск asyncio loop
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def call_async(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    async def _send_async(self, conn, msg: dict) -> None:
        payload = json.dumps(msg).encode('utf-8')
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, conn.sendall, payload)
        except Exception as e:
            print(f"[SFU] send_async error: {e}")

    @staticmethod
    async def _wait_ice_gathering(pc, timeout: float = WEBRTC_ICE_TIMEOUT) -> None:
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

        Порядок video треков в on_track:
          1-й video → video_hq (DXCamTrack)
          2-й video → video_lq (DXCamTrackLQ)
        Аудио → 'audio'.

        Счётчик _video_count[] отслеживает порядок в замыкании.
        """
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
            'pc':            pc,
            'relay':         relay,
            'tracks':        {},
            'source_tracks': {},
            'conn':          conn,
        }
        self._streamer_entries[streamer_uid] = entry

        # Счётчик video-треков в замыкании: 0=HQ, 1=LQ
        _video_count = [0]

        @pc.on("track")
        def on_track(track):
            if track.kind == 'video':
                key = 'video_hq' if _video_count[0] == 0 else 'video_lq'
                _video_count[0] += 1
                print(
                    f"[SFU] Стример uid={streamer_uid}: "
                    f"video трек #{_video_count[0]-1} → {key}"
                )
            else:
                key = track.kind   # 'audio'
                print(
                    f"[SFU] Стример uid={streamer_uid}: "
                    f"✔ audio трек получен"
                )

            relayed = relay.subscribe(track, buffered=False)
            entry['tracks'][key]        = relayed
            entry['source_tracks'][key] = track

            # ── Флаш pending-зрителей ────────────────────────────────────────
            #
            # Аудио трек пришёл → все треки готовы → флашим немедленно.
            # video_hq пришёл → ставим отложенный флаш через 500 мс:
            #   RadminVPN может давать задержку между треками до 300мс.
            #   Если аудио придёт за это время — флаш выше отработает раньше.
            #   Если нет (stream_audio=False) — флашим без аудио через 500мс.
            #
            if key == 'audio':
                pending = self._pending_viewers.pop(streamer_uid, [])
                for v_uid, v_conn, v_quality in pending:
                    asyncio.ensure_future(
                        self.handle_viewer_connect(v_uid, streamer_uid, v_conn, v_quality)
                    )
                if pending:
                    print(
                        f"[SFU] Стример uid={streamer_uid}: аудио готов — "
                        f"флаш {len(pending)} pending-зрителей"
                    )

            elif key == 'video_hq':
                # Отложенный флаш: 500 мс на приход LQ + аудио-трека
                async def _fallback_flush(s_uid=streamer_uid):
                    await asyncio.sleep(0.50)
                    pending = self._pending_viewers.pop(s_uid, [])
                    if pending:
                        print(
                            f"[SFU] Стример uid={s_uid}: отложенный флаш "
                            f"(аудио не пришло за 500мс) — {len(pending)} зрителей"
                        )
                        for v_uid, v_conn, v_quality in pending:
                            asyncio.ensure_future(
                                self.handle_viewer_connect(v_uid, s_uid, v_conn, v_quality)
                            )
                asyncio.ensure_future(_fallback_flush())

        @pc.on("icecandidate")
        def on_ice(candidate):
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

        try:
            await pc.setRemoteDescription(
                RTCSessionDescription(sdp=sdp, type=sdp_type)
            )
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
    # Зритель: создание offer с нужным качеством (HQ или LQ)
    # ------------------------------------------------------------------

    async def handle_viewer_connect(
        self,
        viewer_uid:   int,
        streamer_uid: int,
        viewer_conn,
        quality:      str = 'hq',
    ) -> None:
        """
        Создаёт WebRTC соединение для зрителя.

        quality='hq' → маршрутизируем video_hq трек (default).
        quality='lq' → маршрутизируем video_lq трек (слабый зритель).

        Fallback: если LQ трек не пришёл от стримера (старая версия клиента
        без simulcast) → всем зрителям отдаём video_hq.

        Аудиотрек общий для всех зрителей — не зависит от quality.

        КРИТИЧНО: relay.subscribe() заново для каждого зрителя.
        Нельзя переиспользовать relayed_track из entry['tracks'] —
        при закрытии старого viewer PC aiortc вызывает track.stop() →
        relayed_track.readyState = 'ended' → нулевой RTP поток.
        """
        entry = self._streamer_entries.get(streamer_uid)

        # Проверяем, что нужный трек уже готов
        hq_ready = entry is not None and 'video_hq' in entry.get('source_tracks', {})
        if not hq_ready:
            self._pending_viewers.setdefault(streamer_uid, []).append(
                (viewer_uid, viewer_conn, quality)
            )
            print(
                f"[SFU] Зритель uid={viewer_uid}: стример uid={streamer_uid} "
                f"ещё не готов — помещён в pending (quality={quality})"
            )
            return

        old = self._viewer_entries.pop(viewer_uid, None)
        if old:
            try:
                await old['pc'].close()
            except Exception:
                pass

        cfg = RTCConfiguration(iceServers=[])
        pc  = RTCPeerConnection(cfg)

        source_tracks = entry.get('source_tracks', {})
        relay         = entry['relay']

        # Выбираем video ключ по quality; fallback на video_hq
        video_key = 'video_lq' if quality == 'lq' else 'video_hq'
        if video_key not in source_tracks:
            video_key = 'video_hq'   # LQ не пришёл → даём HQ
            if quality == 'lq':
                print(
                    f"[SFU] Зритель uid={viewer_uid}: LQ трек недоступен "
                    f"(стример без simulcast) — используем HQ"
                )

        for key, source_track in source_tracks.items():
            if key == video_key or key == 'audio':
                fresh = relay.subscribe(source_track, buffered=False)
                pc.addTrack(fresh)

        self._viewer_entries[viewer_uid] = {
            'pc':           pc,
            'conn':         viewer_conn,
            'streamer_uid': streamer_uid,
            'quality':      video_key,
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
                f"[SFU] Offer → зритель uid={viewer_uid} "
                f"(стример uid={streamer_uid}, quality={video_key})"
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
        entry = self._viewer_entries.get(viewer_uid)
        if entry is None:
            print(f"[SFU] handle_viewer_answer: нет PC для uid={viewer_uid}")
            return
        try:
            await entry['pc'].setRemoteDescription(
                RTCSessionDescription(sdp=sdp, type=sdp_type)
            )
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
        pc = None
        if uid in self._streamer_entries:
            pc = self._streamer_entries[uid]['pc']
        elif uid in self._viewer_entries:
            pc = self._viewer_entries[uid]['pc']

        if pc is None:
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
        entry = self._streamer_entries.pop(streamer_uid, None)
        if entry:
            try:
                await entry['pc'].close()
            except Exception:
                pass
            print(f"[SFU] Стример uid={streamer_uid}: PC закрыт")

        victims = [
            v_uid for v_uid, ve in list(self._viewer_entries.items())
            if ve['streamer_uid'] == streamer_uid
        ]
        for v_uid in victims:
            await self.close_viewer(v_uid)

        self._pending_viewers.pop(streamer_uid, None)

    async def close_viewer(self, viewer_uid: int) -> None:
        entry = self._viewer_entries.pop(viewer_uid, None)
        if entry:
            try:
                await entry['pc'].close()
            except Exception:
                pass
            print(f"[SFU] Зритель uid={viewer_uid}: PC закрыт")

    # ------------------------------------------------------------------
    # Остановка SFU
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        if self._loop is None or self._loop.is_closed():
            return

        async def _close_all():
            for uid in list(self._streamer_entries.keys()):
                await self.close_streamer(uid)
            for uid in list(self._viewer_entries.keys()):
                await self.close_viewer(uid)
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