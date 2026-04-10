r"""
media_engine_bridge.py — Python-мост к Rust Media Engine v3 (sidecar-процесс)

─── FIX: авто-рестарт SFU при WEBRTC_OFFER ─────────────────────────────────
  Старый код: если SFU не запущен при WEBRTC_OFFER — молча дропал offer.
  Новый код:  пробует перезапустить SFU (до 2 попыток), затем форвардит offer.

  Причина: SFU мог упасть из-за занятого порта (зомби предыдущей сессии).
  После первого запуска sfu_bridge.py убивает зомби (fix там), но при старте
  embedded-сервера SFU и media-engine стартуют почти одновременно —
  race condition. Авто-рестарт здесь закрывает это окно.
"""

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


class MediaEngineBridge:
    """
    Управляет Rust Media Engine (media-engine.exe) как дочерним процессом.
    JSON-команды/события — stdin/stdout.
    WebRTC сигнализация — через SfuBridge (Go Pion SFU).
    """

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

    # ── Жизненный цикл ───────────────────────────────────────────────────────

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
        if not self._running:
            return
        self._running = False
        try:
            self.send_command({"cmd": "SHUTDOWN"})
        except Exception:
            pass
        if self._process is not None:
            try:
                self._process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2.0)
            code = self._process.returncode
            self._process = None
            self._on_exit(code)

    def is_running(self) -> bool:
        return (
            self._running
            and self._process is not None
            and self._process.poll() is None
        )

    # ── IPC ──────────────────────────────────────────────────────────────────

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

    # ── WebRTC сигнализация (внутреннее) ─────────────────────────────────────

    def _handle_webrtc_offer(self, sdp: str) -> None:
        """
        Rust прислал WEBRTC_OFFER (gather-complete SDP).
        1. POST sdp → Pion SFU /streamer/offer → answer SDP
        2. WEBRTC_ANSWER → Rust stdin

        FIX: если SFU не запущен (упал из-за zombie порта при старте),
        пробуем перезапустить до 2 раз перед тем как роняем offer.
        """
        if self._sfu_bridge is None:
            logger.error("[Bridge] WEBRTC_OFFER: sfu_bridge не задан")
            return

        # FIX: авто-рестарт SFU если он упал
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

    # ── Чтение stdout ────────────────────────────────────────────────────────

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
                    # STREAM_STARTED, STREAM_STOPPED, STATS, ERROR
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
        """
        Читает логи Rust media-engine из stderr.
        Дополнительно парсит строки [DLL-DIAG] для watchdog аудио-захвата:
        если RMS=0.0000 и peak=0.0000 удерживается > DLL_SILENCE_TIMEOUT секунд —
        шлём RESTART_CAPTURE команду в Rust.

        Устраняет баг из логов 03:16:25–03:17:08: DLL Capture завис,
        RMS/peak упали в 0, что вызвало burst статичных кадров и PLI-шторм.
        """
        DLL_SILENCE_TIMEOUT = 3.0
        _dll_zero_since: 'float | None' = None

        import time as _time_mod
        import re as _re
        _dll_diag_re = _re.compile(r'\[DLL-DIAG\].*?RMS=([\d.]+).*?peak=([\d.]+)')

        try:
            for raw in iter(self._process.stderr.readline, b""):
                if not self._running:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    self._on_log(line)

                # ── DLL-DIAG watchdog ──────────────────────────────────────
                m = _dll_diag_re.search(line)
                if m:
                    rms  = float(m.group(1))
                    peak = float(m.group(2))
                    now  = _time_mod.time()
                    if rms == 0.0 and peak == 0.0:
                        if _dll_zero_since is None:
                            _dll_zero_since = now
                        elif now - _dll_zero_since >= DLL_SILENCE_TIMEOUT:
                            logger.warning(
                                "[Bridge] DLL Capture RMS=0 уже %.1f сек — рестарт захвата",
                                now - _dll_zero_since,
                            )
                            try:
                                self.send_command({"cmd": "RESTART_CAPTURE"})
                            except Exception as e:
                                logger.warning("[Bridge] RESTART_CAPTURE error: %s", e)
                            _dll_zero_since = now  # не спамим рестартами
                    else:
                        _dll_zero_since = None  # звук есть — сбрасываем
        except Exception:
            pass