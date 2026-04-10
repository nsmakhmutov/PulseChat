# network_engine/webrtc.py — WebRTC стриминг и просмотр + ABR
#
# Миксин WebRTCMixin: стриминг через Rust Media Engine + Pion SFU,
# просмотр через aiortc viewer PC.
# ABR: фоновый поток опрашивает SFU /stats/loss, управляет битрейтом.

import asyncio
import threading
import time

import numpy as np

from config import (
    CMD_WEBRTC_OFFER, CMD_WEBRTC_ANSWER, CMD_WEBRTC_ICE,
    WEBRTC_ICE_TIMEOUT,
    get_bitrate_for_resolution,
)

from .sdp_utils import normalize_sdp_ice, patch_audio_bitrate, patch_opus_fec

# ── Опциональный aiortc ──────────────────────────────────────────────────────
try:
    from aiortc import (
        RTCPeerConnection, RTCSessionDescription,
        RTCConfiguration,
    )
    AIORTC_AVAILABLE = True
except ImportError:
    AIORTC_AVAILABLE = False
    RTCPeerConnection = None
    RTCSessionDescription = None
    RTCConfiguration = None
    print("[Net] WARNING: aiortc не установлен — WebRTC функции недоступны")

try:
    from audio_engine.audio_capture import SystemAudioTrack
    SYSTEM_AUDIO_AVAILABLE = True
except ImportError:
    SystemAudioTrack = None
    SYSTEM_AUDIO_AVAILABLE = False
    print("[Net] WARNING: SystemAudioTrack недоступен — системный звук в стриме отключён")

try:
    from media_engine_bridge import MediaEngineBridge
    MEDIA_ENGINE_AVAILABLE = True
except ImportError:
    MediaEngineBridge = None
    MEDIA_ENGINE_AVAILABLE = False
    print("[Net] WARNING: MediaEngineBridge недоступен — Rust capture отключён")


# ── ABR константы ─────────────────────────────────────────────────────────────

ABR_POLL_INTERVAL   = 0.5    # было 3.0 — в 6× быстрее реакция
ABR_LOSS_HIGH       = 4.0    # было 5.0 — реагируем раньше
ABR_LOSS_LOW        = 0.5    # было 1.0 — поднимаем только при почти чистом канале
ABR_DECREASE_FACTOR = 0.85   # было 0.75 — менее агрессивное снижение
ABR_INCREASE_FACTOR = 1.05   # было 1.10 — медленнее поднимаем (меньше осцилляций)
ABR_MIN_BITRATE     = 500_000
ABR_MAX_BITRATE     = 20_000_000

# Cooldown: минимальный интервал между двумя изменениями битрейта.
# Нужен чтобы ABR не «пилил» битрейт туда-обратно при кратковременных
# пиках потерь (например, одиночный burst при загрузке страницы).
ABR_COOLDOWN_SECS   = 2.0


class WebRTCMixin:
    """Методы WebRTC стриминга и просмотра."""

    # ------------------------------------------------------------------
    # Инициализация WebRTC атрибутов (вызывается из __init__ NetworkClient)
    # ------------------------------------------------------------------
    def _init_webrtc_attrs(self):
        self._webrtc_loop = None
        self._viewer_pc = None
        self._viewer_answer_future = None
        self._system_audio_track = None
        self._audio_streamer_pc = None
        self._stream_audio_task = None
        self._watching_streamer_uid = 0

        # ── ABR состояние ─────────────────────────────────────────────────
        self._abr_running = False
        self._abr_thread = None
        self._abr_current_bitrate = 6_000_000
        self._abr_max_bitrate = 6_000_000  # устанавливается при старте стрима

        # ── SFU и Media Engine bridges ─────────────────────────────────────
        self._sfu_bridge = None
        try:
            from sfu_bridge import get_shared as _get_sfu
            self._sfu_bridge = _get_sfu(
                on_log=lambda s: print(f"[SFU] {s}"),
                on_exit=lambda c: print(f"[SFU] завершён (code={c})"),
            )
        except ImportError:
            print("[Net] WARNING: SfuBridge недоступен")

        self._media_bridge = None
        if MEDIA_ENGINE_AVAILABLE:
            self._media_bridge = MediaEngineBridge(
                sfu_bridge=self._sfu_bridge,
                on_event=self._handle_media_event,
                on_log=lambda s: print(f"[Media] {s}"),
                on_exit=lambda c: print(f"[Media] процесс завершён (code={c})"),
            )

    # ------------------------------------------------------------------
    # WebRTC asyncio loop
    # ------------------------------------------------------------------
    def _start_webrtc_loop(self) -> None:
        if self._webrtc_loop is not None and not self._webrtc_loop.is_closed():
            return

        self._webrtc_loop = asyncio.new_event_loop()

        if self.video is not None:
            self.video.set_webrtc_loop(self._webrtc_loop)

        t = threading.Thread(
            target=self._webrtc_loop.run_forever,
            daemon=True,
            name="webrtc-asyncio",
        )
        t.start()
        print("[Net] WebRTC asyncio loop запущен")

    def _run_in_webrtc_loop(self, coro):
        if self._webrtc_loop is None or self._webrtc_loop.is_closed():
            print("[Net] WebRTC loop не готов — команда проигнорирована")
            return None
        return asyncio.run_coroutine_threadsafe(coro, self._webrtc_loop)

    # ------------------------------------------------------------------
    # Стриминг — WebRTC
    # ------------------------------------------------------------------
    def start_streaming_webrtc(self, settings: dict | None = None) -> None:
        s = settings or {}

        if self._sfu_bridge is not None and not self._sfu_bridge.is_running():
            print("[Net] Запускаем Pion SFU (lazy, стрим)...")
            if not self._sfu_bridge.start():
                print("[Net] Ошибка запуска SFU — стрим отменён")
                return

        if self._media_bridge is not None and not self._media_bridge.is_running():
            print("[Net] Запускаем Rust Media Engine...")
            if not self._media_bridge.start():
                print("[Net] Ошибка запуска Media Engine — стрим отменён")
                return

        if self._media_bridge is None or not self._media_bridge.is_running():
            print("[Net] start_streaming_webrtc: Media Engine недоступен")
            return

        width   = s.get("width", 1280)
        height  = s.get("height", 720)
        fps     = s.get("fps", 30)
        bitrate = get_bitrate_for_resolution(width, height)

        # Запоминаем максимальный битрейт для ABR
        self._abr_max_bitrate = bitrate
        self._abr_current_bitrate = bitrate

        print(f"[Net] START_STREAM: {width}×{height} @ {fps} fps, {bitrate//1000} kbps")

        self._media_bridge.start_stream(
            monitor=s.get("monitor_idx", 0),
            width=width,
            height=height,
            fps=fps,
            bitrate=bitrate,
            simulcast=False,
            stream_audio=False,
        )

        # Запускаем Python-сторонний захват системного звука
        if s.get("stream_audio", False):
            if SystemAudioTrack is not None:
                self._run_in_webrtc_loop(self._start_audio_stream_coro(s))
            else:
                print("[Net] stream_audio=True, но SystemAudioTrack недоступен")

        # ── Запускаем ABR поток ───────────────────────────────────────────
        self._start_abr()

    def _start_abr(self) -> None:
        """Запускает фоновый ABR-поток."""
        if self._abr_running:
            return
        self._abr_running = True
        self._abr_thread = threading.Thread(
            target=self._abr_loop,
            daemon=True,
            name="abr-loop",
        )
        self._abr_thread.start()
        print(f"[ABR] Запущен (poll={ABR_POLL_INTERVAL}s, cooldown={ABR_COOLDOWN_SECS}s)")

    def _stop_abr(self) -> None:
        self._abr_running = False
        self._abr_thread = None
        print("[ABR] Остановлен")

    def _abr_loop(self) -> None:
        """
        Adaptive Bitrate: опрашивает SFU /stats/loss каждые ABR_POLL_INTERVAL сек.

        Логика:
          - avg_loss > ABR_LOSS_HIGH  → снижаем на DECREASE_FACTOR
          - avg_loss < ABR_LOSS_LOW   → поднимаем на INCREASE_FACTOR (если < max)
          - cooldown: не меняем чаще чем раз в ABR_COOLDOWN_SECS

        Изменения ≤ 20% обрабатываются Rust как "soft" (только IDR, без rebuild).
        Изменения > 20% для NVENC/AMF — in-place AVCodecContext update.
        Изменения > 20% для libx264/QSV — полный rebuild энкодера.
        """
        _last_change_ts: float = 0.0

        while self._abr_running:
            time.sleep(ABR_POLL_INTERVAL)

            if not self._abr_running:
                break

            try:
                if self._sfu_bridge is None or not self._sfu_bridge.is_running():
                    continue

                stats = self._sfu_bridge.get_loss_stats()
                if stats is None:
                    continue

                # Нет зрителей — нечего адаптировать
                if stats.get('viewers', 0) == 0:
                    continue

                avg_loss   = stats.get('avg_loss_pct',  0.0)
                avg_jitter = stats.get('avg_jitter_ms', 0.0)
                old_br     = self._abr_current_bitrate
                new_br     = old_br

                if avg_loss > ABR_LOSS_HIGH:
                    new_br = max(ABR_MIN_BITRATE, int(old_br * ABR_DECREASE_FACTOR))
                elif avg_loss < ABR_LOSS_LOW and old_br < self._abr_max_bitrate:
                    new_br = min(self._abr_max_bitrate, int(old_br * ABR_INCREASE_FACTOR))

                if new_br == old_br:
                    continue

                # Cooldown: защита от быстрых осцилляций
                now = time.time()
                if now - _last_change_ts < ABR_COOLDOWN_SECS:
                    continue

                _last_change_ts = now
                self._abr_current_bitrate = new_br

                if self._media_bridge is not None and self._media_bridge.is_running():
                    self._media_bridge.set_bitrate(new_br, new_br // 4)

                if hasattr(self, 'bitrate_adjusted'):
                    self.bitrate_adjusted.emit(new_br)

                direction = "↓" if new_br < old_br else "↑"
                print(
                    f"[ABR] {direction} {old_br // 1000}→{new_br // 1000} kbps "
                    f"(loss={avg_loss:.1f}%, jitter={avg_jitter:.1f}ms)"
                )

            except Exception as e:
                print(f"[ABR] Ошибка: {e}")

    async def _start_audio_stream_coro(self, settings: dict) -> None:
        """
        Запускает Python-сторонний захват системного звука через
        InPulseAudioExclusion.dll и подключает его к Pion SFU.
        FEC включён через SDP patching.
        """
        from aiortc import RTCPeerConnection, RTCConfiguration, RTCSessionDescription

        if SystemAudioTrack is None:
            print("[Net] SystemAudioTrack недоступен — audio стрим невозможен")
            return

        if self._sfu_bridge is None or not self._sfu_bridge.is_running():
            print("[Net] SFU не запущен — audio стрим отменён")
            return

        if self._audio_streamer_pc is not None:
            try:
                await self._audio_streamer_pc.close()
            except Exception:
                pass
            self._audio_streamer_pc = None

        if self._system_audio_track is not None:
            try:
                self._system_audio_track.stop()
            except Exception:
                pass
            self._system_audio_track = None

        print("[Net] [AudioStream] Создаём aiortc PC (sendonly audio)...")

        cfg = RTCConfiguration(iceServers=[])
        pc = RTCPeerConnection(cfg)
        self._audio_streamer_pc = pc

        audio_track = SystemAudioTrack(
            device_idx=settings.get("system_audio_device"),
            audio_handler=self.audio,
        )
        self._system_audio_track = audio_track
        pc.addTrack(audio_track)

        try:
            from config import STREAM_AUDIO_BITRATE as _sa_br
        except (ImportError, AttributeError):
            _sa_br = 128000  # Повышен до 128kbps для лучшего качества

        try:
            offer = await pc.createOffer()

            # Патчим SDP: битрейт + FEC
            patched_sdp = patch_audio_bitrate(offer.sdp, _sa_br // 1000)
            patched_sdp = patch_opus_fec(patched_sdp)
            patched_offer = RTCSessionDescription(sdp=patched_sdp, type=offer.type)

            await pc.setLocalDescription(patched_offer)

            print(f"[Net] [AudioStream] offer создан (битрейт: {_sa_br // 1000} kbps, FEC=on)")
            await self._wait_ice_gathering(pc)

            offer_sdp = normalize_sdp_ice(pc.localDescription.sdp)

            loop = asyncio.get_running_loop()
            answer_sdp = await loop.run_in_executor(
                None,
                self._sfu_bridge.post_streamer_audio_offer,
                offer_sdp,
            )

            await asyncio.wait_for(
                pc.setRemoteDescription(
                    RTCSessionDescription(sdp=answer_sdp, type="answer")
                ),
                timeout=10.0,
            )
            print("[Net] [AudioStream] Звук успешно запущен")

        except Exception as e:
            import traceback as _tb
            print(f"[Net] [AudioStream] Ошибка запуска: {e}")
            print(_tb.format_exc())
            if self._system_audio_track:
                self._system_audio_track.stop()
            await pc.close()
            self._system_audio_track = None
            self._audio_streamer_pc = None

    async def _handle_streamer_answer_coro(self, sdp: str, sdp_type: str) -> None:
        print("[Net] _handle_streamer_answer: игнорируем (v3: Rust webrtc-rs)")

    def stop_streaming_webrtc(self) -> None:
        # Останавливаем ABR
        self._stop_abr()

        if self._media_bridge is not None and self._media_bridge.is_running():
            self._media_bridge.stop_stream()
            print("[Net] Rust capture: STOP_STREAM отправлен")

        import time as _t
        _t.sleep(0.1)

        if self._media_bridge is not None and self._media_bridge.is_running():
            self._media_bridge.stop()
            print("[Net] media-engine.exe: завершён принудительно")

        if self._sfu_bridge is not None:
            self._sfu_bridge.stop()
            print("[Net] sidecar.exe: завершён принудительно")

        if self._audio_streamer_pc is not None:
            if self._webrtc_loop is not None and not self._webrtc_loop.is_closed():
                self._run_in_webrtc_loop(self._close_pc_coro(self._audio_streamer_pc))
            self._audio_streamer_pc = None

        if self._sfu_bridge is not None and self._sfu_bridge.is_running():
            try:
                self._sfu_bridge.delete_audio_streamer()
            except Exception as e:
                print(f"[Net] delete_audio_streamer error: {e}")

        if self._system_audio_track is not None:
            try:
                self._system_audio_track.stop()
            except Exception as e:
                print(f"[Net] SystemAudioTrack stop error: {e}")
            self._system_audio_track = None

        if self.video:
            self.video.stop_streaming()

    # ------------------------------------------------------------------
    # Rust Media Engine: обработка событий
    # ------------------------------------------------------------------
    def _handle_media_event(self, event: dict) -> None:
        ev = event.get('event', '')
        if ev == 'STREAM_STARTED':
            print(
                f"[Net] Rust стрим: {event.get('encoder', '?')} "
                f"{event.get('width', 0)}×{event.get('height', 0)} "
                f"@ {event.get('fps', 0)} fps"
            )
        elif ev == 'STREAM_STOPPED':
            print("[Net] Rust стрим остановлен")
        elif ev == 'STATS':
            pass
        elif ev == 'ERROR':
            print(f"[Net] Rust Media Engine ERROR: {event.get('message', '?')}")

    # ------------------------------------------------------------------
    # Просмотр стрима
    # ------------------------------------------------------------------
    def start_watching(self, streamer_uid: int, quality: str = 'hq'):
        self._watching_streamer_uid = streamer_uid
        self.send_json({
            'action':       'stream_watch_start',
            'streamer_uid':  streamer_uid,
            'quality':       quality,
        })
        print(f"[Net] start_watching → streamer_uid={streamer_uid}, quality={quality}")

    def stop_watching(self):
        streamer_uid = self._watching_streamer_uid
        self._watching_streamer_uid = 0

        if self._stream_audio_task is not None:
            try:
                self._stream_audio_task.cancel()
            except Exception:
                pass
            self._stream_audio_task = None

        if self._viewer_pc is not None:
            self._run_in_webrtc_loop(self._close_pc_coro(self._viewer_pc))
            self._viewer_pc = None

        if self._sfu_bridge is not None and self._sfu_bridge.is_running():
            viewer_id = str(getattr(self.audio, 'my_uid', 0) or 0)
            self._sfu_bridge.delete_viewer(viewer_id)

        if streamer_uid:
            self.send_json({
                'action':      'stream_watch_stop',
                'streamer_uid': streamer_uid,
            })

        if self.video and streamer_uid:
            self.video.stop_viewer_for_uid(streamer_uid)

        if self.audio is not None and hasattr(self.audio, 'stop_stream_playback'):
            self.audio.stop_stream_playback()

    def _restart_watching(self, streamer_uid: int) -> None:
        """
        FIX: Автоматический перезапуск просмотра когда стример переподключился.
        Вызывается при получении 'streamer_reconnected' от сервера.

        Последовательность:
          1. Закрываем старый _viewer_pc (он привязан к упавшему ICE-соединению).
          2. Сбрасываем SFU viewer-сессию (иначе Pion SFU откажет в новом offer).
          3. Через 500 мс вызываем start_watching() — новый WebRTC handshake.

        500 мс задержка нужна чтобы:
          - SFU успел удалить старую viewer-сессию (асинхронная операция в Go)
          - media-engine.exe стримера успел пересоединиться с SFU
        """
        print(f"[Net] _restart_watching: стример uid={streamer_uid} переподключился → перезапуск")

        # 1. Закрываем старый viewer PC
        if self._viewer_pc is not None:
            self._run_in_webrtc_loop(self._close_pc_coro(self._viewer_pc))
            self._viewer_pc = None

        # 2. Сбрасываем audio
        if self._stream_audio_task is not None:
            try:
                self._stream_audio_task.cancel()
            except Exception:
                pass
            self._stream_audio_task = None
        if self.audio is not None and hasattr(self.audio, 'stop_stream_playback'):
            self.audio.stop_stream_playback()

        # 3. Удаляем старую SFU viewer-сессию
        if self._sfu_bridge is not None and self._sfu_bridge.is_running():
            viewer_id = str(getattr(self.audio, 'my_uid', 0) or 0)
            self._sfu_bridge.delete_viewer(viewer_id)

        # 4. Уведомляем VideoEngine чтобы остановил старый VideoReceiver
        if self.video and streamer_uid:
            self.video.stop_viewer_for_uid(streamer_uid)

        # 5. Перезапускаем через 500 мс
        import threading as _threading
        def _delayed_restart():
            import time as _t
            _t.sleep(0.5)
            if self._watching_streamer_uid == streamer_uid:
                print(f"[Net] _restart_watching: запускаем новый offer для uid={streamer_uid}")
                self.start_watching(streamer_uid)

        _threading.Thread(target=_delayed_restart, daemon=True, name="restart-watch").start()

    async def _handle_viewer_offer_coro(
        self, streamer_uid: int, sdp: str = None, sdp_type: str = None
    ) -> None:
        print(f"[Viewer] _handle_viewer_offer_coro START: streamer_uid={streamer_uid}")

        if not AIORTC_AVAILABLE:
            print("[Viewer] aiortc не установлен")
            return

        if self._viewer_pc is not None:
            print("[Viewer] закрываем старый viewer PC")
            try:
                await self._viewer_pc.close()
            except Exception:
                pass
            self._viewer_pc = None

        print("[Viewer] создаём RTCPeerConnection (recvonly)...")
        cfg = RTCConfiguration(iceServers=[])
        pc  = RTCPeerConnection(cfg)
        self._viewer_pc = pc

        pc.addTransceiver("video", direction="recvonly")
        pc.addTransceiver("audio", direction="recvonly")

        @pc.on("track")
        def on_track(track):
            print(
                f"[Viewer] ТРЕК ПОЛУЧЕН: kind={track.kind} "
                f"от стримера uid={streamer_uid}"
            )
            if track.kind == "video" and self.video:
                self.video.add_receiver(streamer_uid, track)
            elif track.kind == "audio":
                self._stream_audio_task = asyncio.ensure_future(
                    self._recv_stream_audio_coro(track, streamer_uid)
                )

        @pc.on("connectionstatechange")
        async def on_state():
            state = pc.connectionState
            ice   = pc.iceConnectionState
            print(f"[Viewer] PC state → {state}  ICE → {ice}")
            if state in ("failed", "disconnected", "closed"):
                if self._viewer_pc is pc:
                    print(f"[Viewer] PC потерян: {state}")
                    self._viewer_pc = None

        try:
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            await self._wait_ice_gathering(pc)

            offer_sdp = normalize_sdp_ice(pc.localDescription.sdp)
            print(f"[Viewer] ICE собран, offer len={len(offer_sdp)}")

            loop = asyncio.get_running_loop()
            self._viewer_answer_future = loop.create_future()

            self.send_json({
                'action': CMD_WEBRTC_OFFER,
                'role':   'viewer_offer',
                'sdp':    offer_sdp,
                'type':   'offer',
            })

            try:
                answer_sdp = await asyncio.wait_for(
                    self._viewer_answer_future, timeout=15.0
                )
            except asyncio.TimeoutError:
                print("[Viewer] TIMEOUT 15s: answer не получен")
                self._viewer_pc = None
                return
            finally:
                self._viewer_answer_future = None

            try:
                await asyncio.wait_for(
                    pc.setRemoteDescription(
                        RTCSessionDescription(sdp=answer_sdp, type="answer")
                    ),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                print("[Viewer] setRemoteDescription завис (>10s)")
                if self._viewer_pc is pc:
                    self._viewer_pc = None
                try:
                    await pc.close()
                except Exception:
                    pass
                return
            print(f"[Viewer] подключён к Pion SFU, streamer_uid={streamer_uid}")

        except Exception as e:
            import traceback
            print(f"[Viewer] EXCEPTION: {e}")
            print(traceback.format_exc())
            if self._viewer_pc is pc:
                try:
                    await pc.close()
                except Exception:
                    pass
                self._viewer_pc = None

    async def _recv_stream_audio_coro(self, track, streamer_uid: int) -> None:
        """Приём аудио-фреймов от стримера."""
        print(f"[Net] StreamAudio receiver запущен для uid={streamer_uid}")
        _first = True
        _frame_count = 0

        import time as _time_mod
        _diag_rms_sum: float = 0.0
        _diag_rms_cnt: int   = 0
        _diag_next_ts: float = _time_mod.perf_counter() + 1.0
        _diag_frames_per_sec: int = 0

        try:
            while True:
                try:
                    frame = await asyncio.wait_for(track.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    print(f"[Net] StreamAudio recv error (uid={streamer_uid}): {e}")
                    break

                try:
                    arr = frame.to_ndarray()
                    if arr.dtype != np.float32:
                        arr = arr.astype(np.float32) / 32768.0

                    sr = frame.sample_rate or 48000

                    if _first:
                        _first = False
                        peak = float(np.abs(arr).max())
                        print(
                            f"[VIEWER-DIAG] StreamAudio: ПЕРВЫЙ ФРЕЙМ uid={streamer_uid} "
                            f"sr={sr} shape={arr.shape} dtype={arr.dtype} peak={peak:.4f}",
                            flush=True,
                        )

                    _frame_count += 1
                    _diag_frames_per_sec += 1

                    flat = arr.flatten().astype(np.float32)
                    _diag_rms_sum += float(np.dot(flat, flat))
                    _diag_rms_cnt += len(flat)

                    _now = _time_mod.perf_counter()
                    if _now >= _diag_next_ts and _diag_rms_cnt > 0:
                        rms  = (_diag_rms_sum / _diag_rms_cnt) ** 0.5
                        peak = float(np.abs(flat).max())
                        print(
                            f"[VIEWER-DIAG] StreamAudio uid={streamer_uid}: "
                            f"RMS={rms:.4f}  peak={peak:.4f}  "
                            f"fps={_diag_frames_per_sec}  total={_frame_count}",
                            flush=True,
                        )
                        _diag_rms_sum      = 0.0
                        _diag_rms_cnt      = 0
                        _diag_frames_per_sec = 0
                        _diag_next_ts      = _now + 1.0

                    if self.audio is not None and hasattr(self.audio, 'add_stream_audio'):
                        self.audio.add_stream_audio(arr, sr, vol=1.0)

                except Exception as e:
                    print(f"[Net] StreamAudio frame decode error (uid={streamer_uid}): {e}")

        finally:
            print(f"[Net] StreamAudio receiver завершён (uid={streamer_uid}, фреймов={_frame_count})")

    async def _handle_ice_candidate_coro(self, role, candidate_dict):
        pass  # v3: gather-complete

    @staticmethod
    async def _close_pc_coro(pc) -> None:
        try:
            await pc.close()
        except Exception:
            pass

    @staticmethod
    async def _wait_ice_gathering(pc, timeout: float = WEBRTC_ICE_TIMEOUT) -> None:
        loop     = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while pc.iceGatheringState != "complete":
            if loop.time() >= deadline:
                print(f"[Net] ICE gathering timeout ({timeout}s) — продолжаем")
                break
            await asyncio.sleep(0.05)

    # ------------------------------------------------------------------
    # process_message dispatch для WebRTC
    # ------------------------------------------------------------------
    def _process_webrtc_message(self, msg: dict, act: str) -> bool:
        """Обрабатывает WebRTC сообщения. Возвращает True если обработано."""

        if act == CMD_WEBRTC_OFFER:
            role         = msg.get('role', '')
            streamer_uid = msg.get('streamer_uid', self._watching_streamer_uid)

            if role == 'viewer' and AIORTC_AVAILABLE:
                self._run_in_webrtc_loop(
                    self._handle_viewer_offer_coro(streamer_uid)
                )
            return True

        elif act == CMD_WEBRTC_ANSWER:
            sdp = msg.get('sdp', '')
            fut = self._viewer_answer_future
            if sdp and fut is not None:
                def _resolve(f=fut, s=sdp):
                    if not f.done(): f.set_result(s)
                if self._webrtc_loop and not self._webrtc_loop.is_closed():
                    self._webrtc_loop.call_soon_threadsafe(_resolve)
            return True

        elif act == CMD_WEBRTC_ICE:
            return True  # v3: gather-complete

        elif act == 'streamer_reconnected':
            # FIX: стример вернулся после разрыва → перезапускаем WebRTC handshake.
            # Без этого зритель навсегда остаётся в "Ожидание видео..." потому что
            # старый _viewer_pc привязан к мёртвому ICE-соединению.
            streamer_uid = msg.get('streamer_uid', 0)
            if streamer_uid and streamer_uid == self._watching_streamer_uid:
                print(f"[Net] streamer_reconnected uid={streamer_uid}")
                self._restart_watching(streamer_uid)
            return True

        return False

    # ------------------------------------------------------------------
    # Остановка WebRTC при завершении
    # ------------------------------------------------------------------
    def _stop_webrtc(self) -> None:
        """Вызывается из stop() NetworkClient."""
        self._stop_abr()

        if self._media_bridge is not None:
            try:
                self._media_bridge.stop()
            except Exception as e:
                print(f"[Net] media_bridge stop error: {e}")

        if self._sfu_bridge is not None:
            self._sfu_bridge.stop()

        if self._webrtc_loop is not None and not self._webrtc_loop.is_closed():
            if self._viewer_pc is not None:
                asyncio.run_coroutine_threadsafe(
                    self._close_pc_coro(self._viewer_pc), self._webrtc_loop
                )
                self._viewer_pc = None

            import time as _t
            _t.sleep(0.2)
            try:
                self._webrtc_loop.call_soon_threadsafe(self._webrtc_loop.stop)
            except Exception:
                pass