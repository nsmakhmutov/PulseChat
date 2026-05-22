
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from urllib import request, error as urllib_error

logger = logging.getLogger(__name__)

try:
    from core.job_object import assign_to_job as _assign_to_job
except ImportError:
    _assign_to_job = lambda proc: False

try:
    from config import SFU_PORT as _DEFAULT_PORT, SFU_EXE_NAME as _DEFAULT_EXE, SFU_PORT_RANGE as _PORT_RANGE
except ImportError:
    _DEFAULT_PORT = 7788
    _DEFAULT_EXE  = "sidecar.exe"
    _PORT_RANGE   = 20


def _find_free_port(start: int = _DEFAULT_PORT, attempts: int = _PORT_RANGE) -> int:
    import socket as _sock
    for port in range(start, start + attempts):
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        try:
            s.bind(("0.0.0.0", port))
            s.close()
            return port
        except OSError:
            s.close()
    raise RuntimeError(
        f"[SfuBridge] нет свободного порта в диапазоне {start}–{start + attempts - 1}"
    )

HTTP_TIMEOUT  = 10.0
READY_TIMEOUT = 8.0


class SfuBridge:

    def __init__(
        self,
        exe_path: str = None,
        port: int = None,
        on_log: Callable[[str], None] = None,
        on_exit: Callable[[int], None] = None,
    ):
        if exe_path is None:
            base = Path(getattr(sys, '_MEIPASS', os.path.dirname(__file__)))
            exe_path = str(base / _DEFAULT_EXE)

        self._exe_path = exe_path
        if port is not None:
            self._port = port
        else:
            try:
                self._port = _find_free_port(_DEFAULT_PORT)
                if self._port != _DEFAULT_PORT:
                    logger.info("[SfuBridge] порт %d занят — зарезервирован %d",
                                _DEFAULT_PORT, self._port)
            except RuntimeError as e:
                logger.warning("%s — fallback на %d", e, _DEFAULT_PORT)
                self._port = _DEFAULT_PORT
        self._base_url = f"http://127.0.0.1:{self._port}"
        self._on_log   = on_log or (lambda s: logger.debug("[SFU] %s", s))
        self._on_exit  = on_exit or (lambda c: None)

        self._process: Optional[subprocess.Popen] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._running  = False
        self._ready    = threading.Event()
        self._version: Optional[str] = None

    @property
    def port(self) -> int:
        return self._port

    def start(self, timeout: float = READY_TIMEOUT) -> bool:
        if self._process is not None:
            if self.is_running():
                logger.info("[SfuBridge] уже запущен")
                return True
            logger.warning("[SfuBridge] процесс умер — перезапускаем")
            self._process = None
            self._running = False

        if not os.path.isfile(self._exe_path):
            logger.error("[SfuBridge] %s не найден", self._exe_path)
            return False

        try:
            free = _find_free_port(self._port)
        except RuntimeError:
            logger.warning("[SfuBridge] диапазон портов занят — пробуем убить зомби")
            self._kill_zombie_on_port()
            free = self._port

        if free != self._port:
            logger.info("[SfuBridge] порт %d занят — используем %d", self._port, free)
            self._port = free
            self._base_url = f"http://127.0.0.1:{self._port}"

        if free == self._port:
            pass

        try:
            self._process = subprocess.Popen(
                [self._exe_path, "--port", str(self._port)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                ),
            )
            _assign_to_job(self._process)
        except Exception as e:
            logger.error("[SfuBridge] Ошибка запуска: %s", e)
            return False

        self._running = True
        self._ready.clear()

        self._stdout_thread = threading.Thread(
            target=self._read_stdout, daemon=True, name="sfu-stdout"
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, daemon=True, name="sfu-stderr"
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

        if self._ready.wait(timeout=timeout):
            logger.info(
                "[SfuBridge] Go SFU v%s готов на порту %d (PID=%d)",
                self._version, self._port, self._process.pid,
            )
            return True
        else:
            logger.error("[SfuBridge] TIMEOUT: SFU не отправил READY за %.1f с", timeout)
            self.stop()
            return False

    def _kill_zombie_on_port(self) -> None:

        if sys.platform != "win32":
            return

        import socket as _sock
        probe = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        try:
            probe.bind(("0.0.0.0", self._port))
            probe.close()
            return
        except OSError:
            probe.close()

        logger.warning("[SfuBridge] порт %d занят, ищем зомби...", self._port)
        pids_to_kill: set[str] = set()

        try:
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                addr_match = (
                    f"0.0.0.0:{self._port}" in line or
                    f"127.0.0.1:{self._port}" in line or
                    f"[::]:{self._port}" in line       # IPv6 any
                )
                if addr_match and "LISTENING" in line:
                    parts = line.split()
                    if parts:
                        pids_to_kill.add(parts[-1])
        except Exception as e:
            logger.warning("[SfuBridge] netstat error: %s", e)

        for pid in pids_to_kill:
            try:
                result = subprocess.run(
                    ["taskkill", "/F", "/PID", pid],
                    capture_output=True, text=True, timeout=5
                )
                logger.warning(
                    "[SfuBridge] убит зомби PID=%s на порту %d: %s",
                    pid, self._port, result.stdout.strip()
                )
            except Exception as e:
                logger.warning("[SfuBridge] taskkill PID=%s error: %s", pid, e)

        if pids_to_kill:
            time.sleep(1.0)
        else:
            logger.warning("[SfuBridge] порт %d занят, PID не найден — ждём 1 с", self._port)
            time.sleep(1.0)

    def stop(self) -> None:
        self._running = False
        if self._process is None:
            return
        if self._process.poll() is not None:
            self._process = None
            return
        try:
            self._process.terminate()
        except Exception:
            pass
        try:
            self._process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            try:
                self._process.kill()
                self._process.wait(timeout=2.0)
            except Exception:
                pass

        code = self._process.poll()
        self._process = None
        self._on_exit(code if code is not None else -1)
        logger.info("[SfuBridge] завершён (code=%s)", code)

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def post_streamer_offer(self, sdp: str) -> str:
        resp = self._post("/streamer/offer", {"sdp": sdp})
        answer = resp.get("sdp", "")
        if not answer:
            raise RuntimeError("SFU вернул пустой answer SDP")
        logger.info("[SfuBridge] streamer offer → answer (len=%d)", len(answer))
        return answer

    def post_viewer_offer(self, viewer_id: str, sdp: str) -> str:
        resp = self._post(f"/viewer/{viewer_id}/offer", {"sdp": sdp})
        answer = resp.get("sdp", "")
        if not answer:
            raise RuntimeError(f"SFU вернул пустой answer SDP для viewer {viewer_id}")
        logger.info(
            "[SfuBridge] viewer %s offer → answer (len=%d)", viewer_id, len(answer)
        )
        return answer

    def delete_streamer(self) -> None:
        try:
            self._request("DELETE", "/streamer")
        except Exception as e:
            logger.warning("[SfuBridge] delete_streamer error: %s", e)

    def post_streamer_audio_offer(self, sdp: str) -> str:
        resp = self._post("/streamer/audio/offer", {"sdp": sdp})
        answer = resp.get("sdp", "")
        if not answer:
            raise RuntimeError("SFU вернул пустой answer SDP для audio streamer")
        logger.info("[SfuBridge] audio streamer offer → answer (len=%d)", len(answer))
        return answer

    def delete_audio_streamer(self) -> None:
        try:
            self._request("DELETE", "/streamer/audio")
        except Exception as e:
            logger.warning("[SfuBridge] delete_audio_streamer error: %s", e)

    def delete_viewer(self, viewer_id: str) -> None:
        try:
            self._request("DELETE", f"/viewer/{viewer_id}")
        except Exception as e:
            logger.warning("[SfuBridge] delete_viewer(%s) error: %s", viewer_id, e)

    def status(self) -> dict:
        try:
            return self._get("/status")
        except Exception as e:
            logger.warning("[SfuBridge] status error: %s", e)
            return {"error": str(e)}

    def health(self) -> bool:
        try:
            resp = self._get("/health")
            return resp.get("ok", False)
        except Exception:
            return False

    def get_loss_stats(self) -> dict | None:
        try:
            return self._get("/stats/loss")
        except Exception:
            return None

    def _post(self, path: str, body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        req = request.Request(
            url=self._base_url + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib_error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"SFU HTTP {e.code} на {path}: {body_text}") from e
        except urllib_error.URLError as e:
            raise RuntimeError(f"SFU недоступен ({path}): {e.reason}") from e

    def _get(self, path: str) -> dict:
        req = request.Request(url=self._base_url + path, method="GET")
        try:
            with request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib_error.HTTPError as e:
            raise RuntimeError(f"SFU HTTP {e.code} на {path}") from e
        except urllib_error.URLError as e:
            raise RuntimeError(f"SFU недоступен ({path}): {e.reason}") from e

    def _request(self, method: str, path: str) -> None:
        req = request.Request(url=self._base_url + path, method=method)
        with request.urlopen(req, timeout=HTTP_TIMEOUT):
            pass

    def _read_stdout(self) -> None:
        try:
            for raw in iter(self._process.stdout.readline, b""):
                if not self._running:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("[SFU] stdout (non-JSON): %s", line)
                    continue

                if evt.get("event") == "READY":
                    self._version = evt.get("version", "?")
                    self._ready.set()
                else:
                    logger.debug("[SFU] event: %s", line)

        except Exception as e:
            if self._running:
                logger.error("[SfuBridge] stdout reader error: %s", e)
        finally:
            if self._running:
                self._running = False
                code = self._process.poll() if self._process else -1
                logger.warning("[SfuBridge] stdout EOF (code=%s)", code)
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

_shared_instance: "SfuBridge | None" = None
_shared_lock = __import__('threading').Lock()


def get_shared(
    exe_path: str = None,
    port: int = None,
    on_log=None,
    on_exit=None,
) -> "SfuBridge":

    global _shared_instance
    if _shared_instance is None:
        with _shared_lock:
            if _shared_instance is None:
                _shared_instance = SfuBridge(
                    exe_path=exe_path,
                    port=port,
                    on_log=on_log or (lambda s: __import__('logging').getLogger(__name__).debug("[SFU] %s", s)),
                    on_exit=on_exit or (lambda c: None),
                )
    return _shared_instance

import atexit as _atexit

def _atexit_cleanup():
    global _shared_instance
    if _shared_instance is not None:
        try:
            if _shared_instance.is_running():
                _shared_instance.stop()
        except Exception:
            pass

_atexit.register(_atexit_cleanup)