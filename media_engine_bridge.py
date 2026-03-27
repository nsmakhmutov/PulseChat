r"""
media_engine_bridge.py — Python-мост к Rust Media Engine v2 (sidecar-процесс)

─── Что изменилось vs v1 ────────────────────────────────────────────────────
  УДАЛЕНО:
    - set_streamer_answer()    ← WebRTC в Rust удалён
    - add_ice_candidate()      ← WebRTC в Rust удалён
    - force_keyframe()         ← больше не нужен (aiortc управляет IDR)
    - on_event OFFER/ANSWER/ICE_CANDIDATE

  ДОБАВЛЕНО:
    - PIPE_READY обработчик   ← Rust сообщает имя Named Pipe
    - RustFramePipeReader      ← читает H.264 кадры из Named Pipe
    - attach_tracks()          ← привязывает RustVideoTrack к пайп-ридеру

─── Архитектура (v2) ────────────────────────────────────────────────────────

  Python (InPulse)                         Rust (media-engine.exe)
  ┌─────────────────────────────────────┐  ┌──────────────────────────────┐
  │  MediaEngineBridge                  │  │  main.rs (capture + encode)  │
  │    send_command() ──stdin (JSON)───►│  │    ├── WGC capture           │
  │    on_event()    ◄──stdout (JSON)───│  │    ├── AMF/NVENC encoder     │
  │                                     │  │    └── Named Pipe writer     │
  │  RustFramePipeReader                │  └──────────────────────────────┘
  │    _connect() ◄────Named Pipe───────┤   \\.\pipe\inpulse-me-{pid}
  │    push → RustVideoTrack.hq/lq      │
  │                                     │
  │  NetworkClient                      │
  │    RustVideoTrack.hq → pc.addTrack  │
  │    RustVideoTrack.lq → pc.addTrack  │
  │    aiortc encodes + sends WebRTC    │
  └─────────────────────────────────────┘
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

from rust_video_track import RustVideoTrack, RustFramePipeReader

logger = logging.getLogger(__name__)


class MediaEngineBridge:
    """
    Управляет Rust Media Engine как дочерним процессом.

    JSON-команды/события — stdin/stdout.
    H.264 кадры — Windows Named Pipe (открывается по событию PIPE_READY).
    """

    def __init__(
        self,
        exe_path: str = None,
        on_event: Callable[[dict], None] = None,
        on_log: Callable[[str], None] = None,
        on_exit: Callable[[int], None] = None,
        env_log_level: str = "info",
    ):
        """
        Args:
            exe_path:       Путь к media-engine.exe.
            on_event:       Callback для JSON-событий (STREAM_STARTED, STATS, ERROR...).
                            OFFER/ANSWER/ICE больше не приходят — WebRTC в Python.
            on_log:         Callback для строк логов из stderr.
            on_exit:        Callback при завершении процесса (returncode).
            env_log_level:  Уровень RUST_LOG.
        """
        if exe_path is None:
            base = Path(getattr(sys, '_MEIPASS', os.path.dirname(__file__)))
            exe_path = str(base / "media-engine.exe")

        self._exe_path       = exe_path
        self._on_event       = on_event or (lambda e: None)
        self._on_log         = on_log or (lambda s: print(f"[Media] {s}"))
        self._on_exit        = on_exit or (lambda c: None)
        self._env_log_level  = env_log_level

        self._process: Optional[subprocess.Popen] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._running = False
        self._ready   = threading.Event()
        self._version: Optional[str] = None

        # Named Pipe и треки
        self._pipe_reader: Optional[RustFramePipeReader] = None
        self._track_hq: Optional[RustVideoTrack] = None
        self._track_lq: Optional[RustVideoTrack] = None
        self._simulcast: bool = False

    # ------------------------------------------------------------------
    # Жизненный цикл
    # ------------------------------------------------------------------

    def start(self, timeout: float = 5.0) -> bool:
        """Запускает Rust-процесс и ждёт события READY."""
        if self._process is not None:
            logger.info("[Bridge] Процесс уже запущен")
            return True

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
            print(f"[Bridge] Media Engine v{self._version} запущен (PID={self._process.pid})")
            return True
        else:
            print("[Bridge] TIMEOUT: Media Engine не отправил READY")
            self.stop()
            return False

    def stop(self) -> None:
        """Корректно останавливает Rust-процесс и Named Pipe reader."""
        if not self._running:
            return

        self._running = False

        # Останавливаем пайп-ридер
        self._stop_pipe_reader()

        # Останавливаем треки
        self._stop_tracks()

        # Отправляем SHUTDOWN
        try:
            self.send_command({"cmd": "SHUTDOWN"})
        except Exception:
            pass

        if self._process is not None:
            try:
                self._process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                print("[Bridge] SHUTDOWN timeout → kill")
                self._process.kill()
                self._process.wait(timeout=2.0)

            returncode = self._process.returncode
            self._process = None
            self._on_exit(returncode)
            print(f"[Media] процесс завершён (code={returncode})")

    def is_running(self) -> bool:
        return (
            self._running
            and self._process is not None
            and self._process.poll() is None
        )

    # ------------------------------------------------------------------
    # Управление треками
    # ------------------------------------------------------------------

    def create_tracks(self, fps: int = 30, simulcast: bool = True) -> tuple:
        """
        Создаёт RustVideoTrack(s) для aiortc.
        Вызывать ПЕРЕД start_stream(), чтобы треки были готовы до прихода кадров.

        Returns:
            (track_hq, track_lq)  — track_lq is None если simulcast=False
        """
        self._stop_tracks()
        self._simulcast = simulcast

        self._track_hq = RustVideoTrack(fps=fps, label="hq")
        self._track_lq = RustVideoTrack(fps=fps, label="lq") if simulcast else None

        return self._track_hq, self._track_lq

    def get_tracks(self) -> tuple:
        """Возвращает текущие треки (track_hq, track_lq)."""
        return self._track_hq, self._track_lq

    def _stop_tracks(self) -> None:
        if self._track_hq is not None:
            self._track_hq.stop()
            self._track_hq = None
        if self._track_lq is not None:
            self._track_lq.stop()
            self._track_lq = None

    # ------------------------------------------------------------------
    # IPC: отправка команд
    # ------------------------------------------------------------------

    def send_command(self, cmd: dict) -> None:
        """Отправляет JSON-команду в stdin Rust-процесса."""
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("Media Engine не запущен")

        line = json.dumps(cmd, ensure_ascii=False) + "\n"
        try:
            self._process.stdin.write(line.encode("utf-8"))
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            logger.error("[Bridge] stdin write error: %s", e)
            self._running = False

    # ------------------------------------------------------------------
    # Удобные методы
    # ------------------------------------------------------------------

    def start_stream(
        self,
        monitor: int = 0,
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
        bitrate: int = 6_000_000,
        simulcast: bool = True,
        stream_audio: bool = False,
    ) -> None:
        """
        Запускает стрим.
        Треки должны быть созданы через create_tracks() до этого вызова.
        """
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
        """Останавливает стрим и закрывает Named Pipe."""
        self._stop_pipe_reader()
        self._stop_tracks()
        try:
            self.send_command({"cmd": "STOP_STREAM"})
        except Exception:
            pass

    def set_bitrate(self, bitrate: int, lq_bitrate: int = 0) -> None:
        """Изменяет битрейт на лету."""
        self.send_command({
            "cmd":        "SET_BITRATE",
            "bitrate":    bitrate,
            "lq_bitrate": lq_bitrate or bitrate // 4,
        })

    # ------------------------------------------------------------------
    # Named Pipe: управление
    # ------------------------------------------------------------------

    def _on_pipe_ready(self, pipe_name: str) -> None:
        """
        Вызывается когда Rust отправил PIPE_READY.
        Запускает RustFramePipeReader в фоновом потоке.
        """
        self._stop_pipe_reader()

        if self._track_hq is None:
            logger.warning(
                "[Bridge] PIPE_READY получен, но треки не созданы. "
                "Вызовите create_tracks() перед start_stream()."
            )
            return

        self._pipe_reader = RustFramePipeReader(
            pipe_name=pipe_name,
            track_hq=self._track_hq,
            track_lq=self._track_lq,
        )
        self._pipe_reader.start()
        logger.info("[Bridge] PipeReader запущен → %s", pipe_name)

    def _stop_pipe_reader(self) -> None:
        if self._pipe_reader is not None:
            self._pipe_reader.stop()
            self._pipe_reader = None

    # ------------------------------------------------------------------
    # Чтение stdout (JSON-события)
    # ------------------------------------------------------------------

    def _read_stdout(self) -> None:
        """Читает JSON-события из stdout Rust-процесса."""
        try:
            for raw_line in iter(self._process.stdout.readline, b""):
                if not self._running:
                    break

                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("[Bridge] Невалидный JSON: %.200s", line)
                    continue

                event_type = event.get("event", "")

                if event_type == "READY":
                    self._version = event.get("version", "?")
                    self._ready.set()

                elif event_type == "PIPE_READY":
                    pipe_name = event.get("pipe_name", "")
                    if pipe_name:
                        self._on_pipe_ready(pipe_name)
                    # PIPE_READY не передаём в on_event — внутренняя деталь

                else:
                    # Передаём остальные события (STREAM_STARTED, STATS, ERROR...)
                    try:
                        self._on_event(event)
                    except Exception as e:
                        logger.error("[Bridge] on_event callback error: %s", e)

        except Exception as e:
            if self._running:
                logger.error("[Bridge] stdout reader error: %s", e)
        finally:
            if self._running:
                self._running = False
                returncode = self._process.poll() if self._process else -1
                print(f"[Bridge] stdout EOF (returncode={returncode})")
                self._on_exit(returncode or 0)

    # ------------------------------------------------------------------
    # Чтение stderr (логи Rust)
    # ------------------------------------------------------------------

    def _read_stderr(self) -> None:
        """Читает логи tracing из stderr."""
        try:
            for raw_line in iter(self._process.stderr.readline, b""):
                if not self._running:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    self._on_log(line)
        except Exception:
            pass
