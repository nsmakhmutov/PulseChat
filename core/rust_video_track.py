import logging
logger = logging.getLogger(__name__)
logger.debug("rust_video_track.py loaded (deprecated in v3 — not used by streamer)")

try:
    from aiortc.mediastreams import VideoStreamTrack as _VST
    _base = _VST
except ImportError:
    _base = object


class RustVideoTrack(_base):
    """Deprecated. Not used in v3 streamer path."""
    kind = "video"

    def __init__(self, fps: int = 30, label: str = "hq"):
        if _base is not object:
            super().__init__()
        self._fps   = fps
        self._label = label
        logger.warning(
            "RustVideoTrack is deprecated in v3 — "
            "Rust sends RTP directly to Pion SFU via webrtc-rs"
        )

    def push_frame_threadsafe(self, data: bytes, is_keyframe: bool = False) -> None:
        pass

    def stop(self) -> None:
        if _base is not object:
            super().stop()


class RustFramePipeReader:
    """Deprecated. Named Pipe removed in v3."""

    def __init__(self, pipe_name: str = "", track_hq=None, track_lq=None):
        logger.warning(
            "RustFramePipeReader is deprecated in v3 — "
            "Named Pipe replaced by webrtc-rs"
        )

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass
