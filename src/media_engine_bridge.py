import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

try:
    from core.job_object import assign_to_job as _assign_to_job
except ImportError:
    _assign_to_job = lambda proc: False


class MediaEngineBridge:
    def __init__(
        self,
        exe_path: str = None,
        sfu_bridge=None,
        on_event: Callable[[dict], None] = None,
        on_log: Callable[[str], None] = None,
        on_exit: Callable[[int], None] = None,
        env_log_level: str = "info",
    ):
        if exe_path is None:
            base = Path(getattr(sys, '_MEIPASS', os.path.dirname(__file__)))
            exe_path = str(base / "media-engine.exe")

        self._exe_path      = exe_path
        self._sfu_bridge    = sfu_bridge
        self._on_event      = on_event or (lambda e: None)
        self._on_log        = on_log or (lambda s: print(f"[Media] {s}"))
        self._on_exit       = on_exit or (lambda c: None)
        self._env_log_level = env_log_level

        self._process: Optional[subprocess.Popen] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._running = False
        self._ready   = threading.Event()
        self._version: Optional[str] = None

    def start(self, timeout: float = 5.0) -> bool:
        if self._process is not None:
            if self.is_running():
                return True
            logger.warning("[Bridge] media-engine умер — перезапускаем")
            self._process = None
            self._running = False

        if not os.path.isfile(self._exe_path):
            logger.error("[Bridge] %s не найден", self._exe_path)
            return False

        env = os.environ.copy()
        env["RUST_LOG"] = self._env_log_level

        try:
            self._process = subprocess.Popen(
                [self._exe_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                ),
            )
            _assign_to_job(self._process)
        except Exception as e:
            logger.error("[Bridge] Ошибка запуска: %s", e)
            return False

        self._running = True
        self._ready.clear()

        self._stdout_thread = threading.Thread(
            target=self._read_stdout, daemon=True, name="media-stdout"
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, daemon=True, name="media-stderr"
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

        if self._ready.wait(timeout=timeout):
            logger.info(
                "[Bridge] Media Engine v%s (PID=%d)",
                self._version, self._process.pid,
            )
            return True
        else:
            logger.error("[Bridge] TIMEOUT: READY не получен")
            self.stop()
            return False

    def stop(self) -> None:
        self._running = False

        if self._process is None:
            return

        if self._process.poll() is not None:
            # Уже мёртв — просто очищаем
            code = self._process.returncode
            self._process = None
            self._on_exit(code if code is not None else -1)
            return

        try:
            self.send_command({"cmd": "SHUTDOWN"})
        except Exception:
            pass

        try:
            self._process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            try:
                self._process.terminate()
            except Exception:
                pass
            try:
                self._process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    self._process.kill()
                    self._process.wait(timeout=2.0)
                except Exception:
                    pass

        code = self._process.returncode if self._process else -1
        self._process = None
        self._on_exit(code if code is not None else -1)
        logger.info("[Bridge] media-engine завершён (code=%s)", code)

    def is_running(self) -> bool:
        return (
            self._running
            and self._process is not None
            and self._process.poll() is None
        )

    def send_command(self, cmd: dict) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("Media Engine не запущен")
        line = json.dumps(cmd, ensure_ascii=False) + "\n"
        try:
            self._process.stdin.write(line.encode("utf-8"))
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            logger.error("[Bridge] stdin write error: %s", e)
            self._running = False

    def start_stream(
        self,
        monitor: int = 0,
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
        bitrate: int = 6_000_000,
        simulcast: bool = False,
        stream_audio: bool = False,
    ) -> None:
        self.send_command({
            "cmd":          "START_STREAM",
            "monitor":      monitor,
            "width":        width,
            "height":       height,
            "fps":          fps,
            "bitrate":      bitrate,
            "simulcast":    simulcast,
            "stream_audio": stream_audio,
        })

    def stop_stream(self) -> None:
        try:
            self.send_command({"cmd": "STOP_STREAM"})
        except Exception:
            pass

    def set_bitrate(self, bitrate: int, lq_bitrate: int = 0) -> None:
        self.send_command({
            "cmd":        "SET_BITRATE",
            "bitrate":    bitrate,
            "lq_bitrate": lq_bitrate or bitrate // 4,
        })

    def restart_capture(self) -> None:
        try:
            self.send_command({"cmd": "RESTART_CAPTURE"})
        except Exception as e:
            logger.warning("[Bridge] RESTART_CAPTURE error: %s", e)

    def _handle_webrtc_offer(self, sdp: str) -> None:
        if self._sfu_bridge is None:
            logger.error("[Bridge] WEBRTC_OFFER: sfu_bridge не задан")
            return

        if not self._sfu_bridge.is_running():
            logger.warning("[Bridge] WEBRTC_OFFER: SFU не запущен, пробуем перезапустить...")
            restarted = False
            for attempt in range(1, 3):  # до 2 попыток
                if self._sfu_bridge.start():
                    logger.info("[Bridge] SFU перезапущен (попытка %d) ✅", attempt)
                    restarted = True
                    break
                logger.warning("[Bridge] SFU restart attempt %d failed", attempt)
                time.sleep(0.5)

            if not restarted:
                logger.error("[Bridge] WEBRTC_OFFER: SFU не удалось перезапустить — offer потерян")
                return

        logger.info("[Bridge] WEBRTC_OFFER → SFU (len=%d)", len(sdp))
        try:
            answer = self._sfu_bridge.post_streamer_offer(sdp)
        except Exception as e:
            logger.error("[Bridge] post_streamer_offer: %s", e)
            return

        logger.info("[Bridge] SFU answer (len=%d) → Rust", len(answer))
        try:
            self.send_command({"cmd": "WEBRTC_ANSWER", "sdp": answer})
        except Exception as e:
            logger.error("[Bridge] send WEBRTC_ANSWER: %s", e)

    def _read_stdout(self) -> None:
        try:
            for raw in iter(self._process.stdout.readline, b""):
                if not self._running:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("[Bridge] non-JSON: %.200s", line)
                    continue

                etype = event.get("event", "")

                if etype == "READY":
                    self._version = event.get("version", "?")
                    self._ready.set()

                elif etype == "WEBRTC_OFFER":
                    sdp = event.get("sdp", "")
                    if sdp:
                        threading.Thread(
                            target=self._handle_webrtc_offer,
                            args=(sdp,),
                            daemon=True,
                            name="offer-handler",
                        ).start()

                else:
                    try:
                        self._on_event(event)
                    except Exception as e:
                        logger.error("[Bridge] on_event: %s", e)

        except Exception as e:
            if self._running:
                logger.error("[Bridge] stdout reader: %s", e)
        finally:
            if self._running:
                self._running = False
                code = self._process.poll() if self._process else -1
                self._on_exit(code or 0)

    def _read_stderr(self) -> None:
        try:
            for raw in iter(self._process.stderr.readline, b""):
                if not self._running:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    self._on_log(line)
        except Exception:
            pass