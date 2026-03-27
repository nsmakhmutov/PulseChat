r"""
rust_video_track.py — aiortc VideoStreamTrack с кадрами из Rust Named Pipe.

Архитектура:
    Rust Named Pipe ──(binary frames)──► RustFrameReader (thread)
                                              │
                                              ▼
                                    asyncio.Queue (per quality)
                                              │
                                              ▼
                                RustVideoTrack.recv() → av.VideoFrame
                                              │
                                              ▼
                                    aiortc RTCRtpSender (H264/libx264)
                                              │
                                              ▼
                                           RTP → SFU

Почему decode+re-encode:
    Rust кодирует в H.264 через AMF/NVENC (hardware, ~0% CPU).
    Python декодирует H.264 → av.VideoFrame (~1-2% CPU).
    aiortc перекодирует av.VideoFrame → H.264 через libx264 (~10-15% CPU).
    Итого: ~12-17% CPU vs. ~80% CPU в старой схеме (dxcam+numpy+PyAV+aiortc).

    В Phase 2 можно заменить на прямую передачу YUV420P через пайп
    (убрать двойное кодирование совсем).
"""

import asyncio
import fractions
import logging
import struct
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import av
    AV_AVAILABLE = True
except ImportError:
    AV_AVAILABLE = False
    logger.warning("PyAV не установлен — RustVideoTrack недоступен")

try:
    from aiortc.mediastreams import VideoStreamTrack
    AIORTC_AVAILABLE = True
except ImportError:
    AIORTC_AVAILABLE = False
    VideoStreamTrack = object
    logger.warning("aiortc не установлен — RustVideoTrack недоступен")


# ─── Константы ───────────────────────────────────────────────────────────────

FRAME_FLAG_KEYFRAME = 0x01
FRAME_FLAG_LQ       = 0x02

PIPE_HEADER_SIZE = 8          # [4 bytes size][1 byte flags][3 bytes reserved]
PIPE_CONNECT_RETRIES = 30     # попыток подключения к пайпу
PIPE_CONNECT_DELAY   = 0.1    # секунд между попытками
QUEUE_MAXSIZE = 10            # максимум кадров в очереди (дроп при переполнении)
RECV_TIMEOUT  = 5.0           # секунд ожидания следующего кадра


# =============================================================================
# RustVideoTrack
# =============================================================================

class RustVideoTrack(VideoStreamTrack):
    """
    aiortc VideoStreamTrack, получающий H.264 кадры из Rust Named Pipe.

    Использование:
        track_hq = RustVideoTrack(fps=30, label="hq")
        track_lq = RustVideoTrack(fps=30, label="lq")

        pc.addTrack(track_hq)
        if simulcast:
            pc.addTrack(track_lq)

        # Когда кадр пришёл из пайпа (из другого потока):
        track_hq.push_frame_threadsafe(h264_bytes, is_keyframe=True)
    """

    kind = "video"

    def __init__(self, fps: int = 30, label: str = "hq"):
        super().__init__()
        self._fps    = fps
        self._label  = label

        # Очередь кадров (bytes = H264 Annex B NAL units)
        self._queue: Optional[asyncio.Queue] = None

        # H.264 декодер (создаётся лениво в recv())
        self._decoder = None

        # Метки времени для aiortc
        self._clock_rate  = 90_000
        self._pts         = 0
        self._pts_step    = self._clock_rate // fps

        # asyncio loop для push_frame_threadsafe
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self._stopped = False

    # ------------------------------------------------------------------
    # Поток: приём кадра из Named Pipe reader (другой поток)
    # ------------------------------------------------------------------

    def push_frame_threadsafe(self, data: bytes, is_keyframe: bool = False) -> None:
        """
        Вызывается из потока Named Pipe reader.
        Кладёт кадр в asyncio-очередь для recv().
        """
        loop = self._loop
        if loop is None or not loop.is_running() or self._stopped:
            return

        queue = self._queue
        if queue is None:
            return

        def _put():
            if queue.full():
                # Дроп старейшего кадра (latency > quality)
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait((data, is_keyframe))
            except asyncio.QueueFull:
                pass

        loop.call_soon_threadsafe(_put)

    # ------------------------------------------------------------------
    # aiortc: получить следующий кадр
    # ------------------------------------------------------------------

    async def recv(self) -> "av.VideoFrame":
        """
        Вызывается aiortc RTCRtpSender в asyncio-потоке.
        Возвращает av.VideoFrame (декодированный из H.264).
        """
        # Инициализация при первом вызове (теперь мы в asyncio-потоке)
        if self._loop is None:
            self._loop = asyncio.get_event_loop()
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)

        while not self._stopped:
            try:
                data, is_keyframe = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=RECV_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.debug("[RustVideoTrack:%s] recv timeout, retrying", self._label)
                continue
            except asyncio.CancelledError:
                raise

            frame = self._decode_nal(data)
            if frame is None:
                continue

            frame.pts       = self._pts
            frame.time_base = fractions.Fraction(1, self._clock_rate)
            self._pts      += self._pts_step
            return frame

        raise asyncio.CancelledError("RustVideoTrack stopped")

    def stop(self) -> None:
        """Останавливает трек (вызов из любого потока)."""
        self._stopped = True
        super().stop()

    # ------------------------------------------------------------------
    # Внутренние утилиты
    # ------------------------------------------------------------------

    def _decode_nal(self, data: bytes) -> Optional["av.VideoFrame"]:
        """Декодирует H.264 Annex B NAL units → av.VideoFrame."""
        if not AV_AVAILABLE:
            return None
        try:
            if self._decoder is None:
                self._decoder = av.CodecContext.create("h264", "r")

            packet = av.Packet(data)
            frames = self._decoder.decode(packet)
            if frames:
                return frames[0]

        except Exception as e:
            logger.warning("[RustVideoTrack:%s] decode error: %s", self._label, e)
            # Сброс декодера при ошибке — следующий IDR восстановит стрим
            self._decoder = None

        return None

    def __repr__(self):
        return f"<RustVideoTrack label={self._label} fps={self._fps}>"


# =============================================================================
# RustFramePipeReader
# =============================================================================

class RustFramePipeReader:
    r"""
    Читает кадры из Windows Named Pipe, созданного Rust Media Engine.

    Формат кадра (бинарный):
        [4 bytes LE: payload_size] [1 byte: flags] [3 bytes: reserved]
        [payload_size bytes: H.264 Annex B NAL units]

    Использование (MediaEngineBridge вызывает это автоматически):
        reader = RustFramePipeReader(
            pipe_name=r"\\.\pipe\inpulse-me-12345",
            track_hq=track_hq,
            track_lq=track_lq,
        )
        reader.start()
        ...
        reader.stop()
    """

    def __init__(
        self,
        pipe_name: str,
        track_hq: Optional[RustVideoTrack] = None,
        track_lq: Optional[RustVideoTrack] = None,
    ):
        self._pipe_name = pipe_name
        self._track_hq  = track_hq
        self._track_lq  = track_lq
        self._running   = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Запускает фоновый поток чтения пайпа."""
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="rust-pipe-reader",
        )
        self._thread.start()
        logger.info("[PipeReader] Запущен → %s", self._pipe_name)

    def stop(self) -> None:
        """Останавливает поток (блокирующий вызов, ≤2 сек)."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        """Основной цикл чтения пайпа."""
        pipe = self._connect()
        if pipe is None:
            logger.error("[PipeReader] Не удалось подключиться к %s", self._pipe_name)
            return

        logger.info("[PipeReader] Подключён к %s", self._pipe_name)
        frames_received = 0

        try:
            while self._running:
                # Читаем заголовок (8 байт)
                header = self._read_exactly(pipe, PIPE_HEADER_SIZE)
                if header is None:
                    break

                size  = struct.unpack_from("<I", header, 0)[0]
                flags = header[4]

                if size == 0 or size > 10 * 1024 * 1024:  # sanity check: max 10 MB
                    logger.warning("[PipeReader] Неверный размер кадра: %d", size)
                    break

                # Читаем payload (H.264 NAL units)
                payload = self._read_exactly(pipe, size)
                if payload is None:
                    break

                is_keyframe = bool(flags & FRAME_FLAG_KEYFRAME)
                is_lq       = bool(flags & FRAME_FLAG_LQ)

                # Пушим кадр в нужный трек
                track = self._track_lq if is_lq else self._track_hq
                if track is not None:
                    track.push_frame_threadsafe(payload, is_keyframe)

                frames_received += 1

        except Exception as e:
            if self._running:
                logger.error("[PipeReader] Ошибка чтения: %s", e)
        finally:
            try:
                pipe.close()
            except Exception:
                pass
            logger.info("[PipeReader] Остановлен. Прочитано кадров: %d", frames_received)

    def _connect(self):
        """Подключается к Named Pipe (с повторными попытками)."""
        for attempt in range(PIPE_CONNECT_RETRIES):
            if not self._running:
                return None
            try:
                # open() работает с Windows Named Pipe напрямую
                pipe = open(self._pipe_name, "rb", buffering=0)
                return pipe
            except FileNotFoundError:
                # Пайп ещё не создан — ждём
                if attempt == 0:
                    logger.debug("[PipeReader] Ожидание пайпа...")
                time.sleep(PIPE_CONNECT_DELAY)
            except Exception as e:
                logger.error("[PipeReader] connect error: %s", e)
                time.sleep(PIPE_CONNECT_DELAY)

        return None

    @staticmethod
    def _read_exactly(pipe, n: int) -> Optional[bytes]:
        """Читает ровно n байт из пайпа (блокирующий)."""
        buf = b""
        while len(buf) < n:
            try:
                chunk = pipe.read(n - len(buf))
                if not chunk:
                    return None
                buf += chunk
            except OSError:
                return None
        return buf
