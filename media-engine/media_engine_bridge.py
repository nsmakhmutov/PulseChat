"""
media_engine_bridge.py — Python-мост к Rust Media Engine (sidecar-процесс)

Заменяет прямое использование VideoEngine.start_streaming() / DXCamTrack.
NetworkClient вызывает MediaEngineBridge вместо создания DXCamTrack.

Архитектура:
    ┌─────────────────────────────────────────────────────┐
    │ Python (InPulse)                                    │
    │                                                     │
    │  MediaEngineBridge                                  │
    │    ├── subprocess.Popen("media-engine.exe")         │
    │    ├── stdin  → JSON команды (START/STOP/ICE/...)   │
    │    ├── stdout ← JSON события (OFFER/ANSWER/STATS)   │
    │    └── stderr ← логи Rust (tracing → stderr)       │
    │                                                     │
    │  NetworkClient                                      │
    │    ├── bridge.start_stream(settings)                │
    │    ├── bridge.on_event → send_json() → SFU          │
    │    └── process_message(CMD_WEBRTC_ANSWER) →         │
    │        bridge.set_streamer_answer(sdp)              │
    └─────────────────────────────────────────────────────┘

Использование:
    from media_engine_bridge import MediaEngineBridge

    bridge = MediaEngineBridge(
        exe_path="./media-engine.exe",
        on_event=self._handle_media_event,
    )
    bridge.start()  # запускает процесс
    bridge.send_command({
        "cmd": "START_STREAM",
        "monitor": 0, "width": 1280, "height": 720,
        "fps": 30, "bitrate": 6000000, "simulcast": True,
    })
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional


class MediaEngineBridge:
    """
    Управляет Rust Media Engine как дочерним процессом.
    Общение: JSON через stdin/stdout, логи через stderr.
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
            exe_path: Путь к media-engine.exe.
                      По умолчанию ищет рядом с текущим скриптом.
            on_event: Callback для JSON-событий из stdout.
            on_log:   Callback для строк логов из stderr.
            on_exit:  Callback при завершении процесса (returncode).
            env_log_level: Уровень логирования Rust (RUST_LOG).
        """
        if exe_path is None:
            # Ищем media-engine.exe рядом с Python-приложением
            base = Path(getattr(sys, '_MEIPASS', os.path.dirname(__file__)))
            exe_path = str(base / "media-engine.exe")

        self._exe_path = exe_path
        self._on_event = on_event or (lambda e: None)
        self._on_log = on_log or (lambda s: print(f"[Rust] {s}"))
        self._on_exit = on_exit or (lambda c: None)
        self._env_log_level = env_log_level

        self._process: Optional[subprocess.Popen] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._running = False
        self._ready = threading.Event()
        self._version: Optional[str] = None

    # ------------------------------------------------------------------
    # Жизненный цикл
    # ------------------------------------------------------------------

    def start(self, timeout: float = 5.0) -> bool:
        """
        Запускает Rust-процесс и ждёт события READY.

        Returns:
            True если процесс запустился и отправил READY.
        """
        if self._process is not None:
            print("[Bridge] Процесс уже запущен")
            return True

        if not os.path.isfile(self._exe_path):
            print(f"[Bridge] ОШИБКА: {self._exe_path} не найден")
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
                # Windows: не показывать окно консоли
                creationflags=(
                    subprocess.CREATE_NO_WINDOW
                    if sys.platform == "win32"
                    else 0
                ),
            )
        except Exception as e:
            print(f"[Bridge] Ошибка запуска: {e}")
            return False

        self._running = True
        self._ready.clear()

        # Потоки чтения stdout и stderr
        self._stdout_thread = threading.Thread(
            target=self._read_stdout, daemon=True, name="media-stdout"
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, daemon=True, name="media-stderr"
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

        # Ждём READY
        if self._ready.wait(timeout=timeout):
            print(
                f"[Bridge] Media Engine v{self._version} запущен "
                f"(PID={self._process.pid})"
            )
            return True
        else:
            print("[Bridge] TIMEOUT: Media Engine не отправил READY")
            self.stop()
            return False

    def stop(self) -> None:
        """Корректно останавливает Rust-процесс."""
        if not self._running:
            return

        self._running = False

        # Отправляем SHUTDOWN
        try:
            self.send_command({"cmd": "SHUTDOWN"})
        except Exception:
            pass

        # Ждём завершения
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
            print(f"[Bridge] Процесс завершён (code={returncode})")

    def is_running(self) -> bool:
        return (
            self._running
            and self._process is not None
            and self._process.poll() is None
        )

    # ------------------------------------------------------------------
    # IPC: отправка команд
    # ------------------------------------------------------------------

    def send_command(self, cmd: dict) -> None:
        """
        Отправляет JSON-команду в stdin Rust-процесса.

        Примеры:
            bridge.send_command({"cmd": "START_STREAM", ...})
            bridge.send_command({"cmd": "STOP_STREAM"})
            bridge.send_command({"cmd": "SHUTDOWN"})
        """
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("Media Engine не запущен")

        line = json.dumps(cmd, ensure_ascii=False) + "\n"
        try:
            self._process.stdin.write(line.encode("utf-8"))
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            print(f"[Bridge] stdin write error: {e}")
            self._running = False

    # ------------------------------------------------------------------
    # Удобные методы (обёртки над send_command)
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
        """Запускает стрим. Эквивалент VideoEngine.start_streaming()."""
        self.send_command({
            "cmd": "START_STREAM",
            "monitor": monitor,
            "width": width,
            "height": height,
            "fps": fps,
            "bitrate": bitrate,
            "simulcast": simulcast,
            "stream_audio": stream_audio,
        })

    def stop_stream(self) -> None:
        """Останавливает стрим."""
        self.send_command({"cmd": "STOP_STREAM"})

    def set_streamer_answer(self, sdp: str, sdp_type: str = "answer") -> None:
        """Передаёт SDP answer от SFU."""
        self.send_command({
            "cmd": "STREAMER_ANSWER",
            "sdp": sdp,
            "type": sdp_type,
        })

    def add_ice_candidate(self, target: str, candidate: dict) -> None:
        """Передаёт ICE candidate."""
        self.send_command({
            "cmd": "ICE_CANDIDATE",
            "target": target,
            "candidate": candidate,
        })

    def force_keyframe(self) -> None:
        """Запрашивает keyframe (для нового зрителя)."""
        self.send_command({"cmd": "FORCE_KEYFRAME"})

    def set_bitrate(self, bitrate: int, lq_bitrate: int = 0) -> None:
        """Изменяет битрейт на лету."""
        self.send_command({
            "cmd": "SET_BITRATE",
            "bitrate": bitrate,
            "lq_bitrate": lq_bitrate or bitrate // 4,
        })

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
                    print(f"[Bridge] Невалидный JSON из stdout: {line[:200]}")
                    continue

                event_type = event.get("event", "")

                # READY — разблокируем start()
                if event_type == "READY":
                    self._version = event.get("version", "?")
                    self._ready.set()

                # Передаём event в callback
                try:
                    self._on_event(event)
                except Exception as e:
                    print(f"[Bridge] on_event callback error: {e}")

        except Exception as e:
            if self._running:
                print(f"[Bridge] stdout reader error: {e}")
        finally:
            # Процесс завершился
            if self._running:
                self._running = False
                returncode = (
                    self._process.poll() if self._process else -1
                )
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


# =============================================================================
# Пример интеграции в NetworkClient
# =============================================================================

"""
Как заменить Python VideoEngine на Rust Media Engine в network_engine.py:

1. В __init__ NetworkClient:

    from media_engine_bridge import MediaEngineBridge

    self._media_bridge = MediaEngineBridge(
        on_event=self._handle_media_event,
        on_log=lambda s: print(f"[Media] {s}"),
    )

2. В connect (после TCP-подключения):

    if not self._media_bridge.is_running():
        self._media_bridge.start()

3. Замена start_streaming_webrtc():

    def start_streaming_webrtc(self, settings=None):
        s = settings or {}
        self._media_bridge.start_stream(
            monitor=s.get("monitor_idx", 0),
            width=s.get("width", 1280),
            height=s.get("height", 720),
            fps=s.get("fps", 30),
            bitrate=get_bitrate_for_resolution(s.get("width", 1280), s.get("height", 720)),
            simulcast=True,
            stream_audio=s.get("stream_audio", False),
        )

4. Обработка событий от Rust:

    def _handle_media_event(self, event: dict):
        ev = event.get("event", "")

        if ev == "OFFER":
            # Пересылаем offer на SFU
            self.send_json({
                'action': CMD_WEBRTC_OFFER,
                'role':   event['role'],
                'sdp':    event['sdp'],
                'type':   event['type'],
            })

        elif ev == "ICE_CANDIDATE":
            self.send_json({
                'action':    CMD_WEBRTC_ICE,
                'role':      'streamer',
                'candidate': event['candidate'],
            })

        elif ev == "STREAM_STARTED":
            print(f"[Media] Стрим: {event['encoder']}, "
                  f"{event['width']}×{event['height']} @ {event['fps']} fps")

        elif ev == "STATS":
            print(f"[Media] FPS={event['fps']}, "
                  f"{event['bitrate_kbps']} kbps, "
                  f"dropped={event['dropped_frames']}")

        elif ev == "ERROR":
            print(f"[Media] ОШИБКА: {event['message']}")

5. Обработка answer от SFU (в process_message):

    elif action == CMD_WEBRTC_ANSWER and msg.get('role') == 'streamer':
        # Было: self._run_in_webrtc_loop(self._handle_streamer_answer_coro(...))
        self._media_bridge.set_streamer_answer(
            sdp=msg['sdp'],
            sdp_type=msg.get('type', 'answer'),
        )

6. Обработка ICE от SFU:

    elif action == CMD_WEBRTC_ICE and msg.get('target') == 'streamer':
        self._media_bridge.add_ice_candidate(
            target='streamer',
            candidate=msg['candidate'],
        )

7. stop_streaming_webrtc():

    def stop_streaming_webrtc(self):
        self._media_bridge.stop_stream()

8. disconnect():

    self._media_bridge.stop()
"""
