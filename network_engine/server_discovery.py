import json
import socket
import threading
import time

from config import DISCOVERY_PORT, DISCOVERY_INTERVAL, DISCOVERY_TIMEOUT

def get_local_radmin_ip() -> str:
    try:
        hostname = socket.gethostname()
        all_ips = socket.gethostbyname_ex(hostname)[2]
        for ip in all_ips:
            if ip.startswith('26.'):
                return ip
        for ip in all_ips:
            if not ip.startswith('127.'):
                return ip
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('26.255.255.255', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'

class ServerAnnouncer:
    _BROADCAST_TARGETS = [
        ('255.255.255.255',  DISCOVERY_PORT),
        ('26.255.255.255',   DISCOVERY_PORT),
    ]

    def __init__(
        self,
        server_ip:       str,
        server_port:     int,
        host_nick:       str,
        server_name:     str = '',
        get_user_count = None,
        get_user_nicks = None,
    ):
        self.server_ip      = server_ip
        self.server_port    = server_port
        self.host_nick      = host_nick
        self.server_name    = server_name or 'InPulse Server'
        self._get_count     = get_user_count or (lambda: 0)
        self._get_nicks     = get_user_nicks or (lambda: [])
        self._running       = False
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None      = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="discovery-announcer"
        )
        self._thread.start()
        print(f"[Discovery] Announcer запущен: IP={self.server_ip}, "
              f"server='{self.server_name}', nick={self.host_nick!r}")

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        print("[Discovery] Announcer остановлен")

    def _loop(self) -> None:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            while self._running:
                _nicks = self._get_nicks()
                _safe_nicks = [n[:16] for n in _nicks[:20]]
                msg = json.dumps({
                    "action":      "server_announce",
                    "ip":          self.server_ip,
                    "port":        self.server_port,
                    "host_nick":   self.host_nick[:16],
                    "server_name": self.server_name,
                    "user_count":  self._get_count(),
                    "user_nicks":  _safe_nicks,
                }).encode('utf-8')

                for target in self._BROADCAST_TARGETS:
                    try:
                        self._sock.sendto(msg, target)
                    except Exception as e:
                        print(f"[Discovery] Announce to {target[0]} error: {e}")
                time.sleep(DISCOVERY_INTERVAL)

        except Exception as e:
            print(f"[Discovery] Announcer fatal error: {e}")
        finally:
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass

class ServerDiscovery:
    def discover(self, timeout: float = DISCOVERY_TIMEOUT) -> dict | None:
        sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('', DISCOVERY_PORT))
            sock.settimeout(timeout)

            while True:
                try:
                    data, _addr = sock.recvfrom(4096)
                    msg = json.loads(data.decode('utf-8'))
                    if msg.get('action') == 'server_announce':
                        ip   = msg.get('ip', '')
                        port = msg.get('port', 5000)
                        nick = msg.get('host_nick', '?')
                        name = msg.get('server_name', 'InPulse Server')
                        cnt  = msg.get('user_count', 0)
                        if ip:
                            print(f"[Discovery] Сервер обнаружен: {ip} (хост: {nick!r})")
                            return {
                                'ip':          ip,
                                'port':        port,
                                'host_nick':   nick,
                                'server_name': name,
                                'user_count':  cnt,
                            }
                except socket.timeout:
                    return None
                except json.JSONDecodeError:
                    continue

        except OSError as e:
            print(f"[Discovery] bind error (порт {DISCOVERY_PORT} занят?): {e}")
            return None
        except Exception as e:
            print(f"[Discovery] Discover error: {e}")
            return None
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

    def discover_all(self, timeout: float = DISCOVERY_TIMEOUT) -> list[dict]:
        found: dict[str, dict] = {}
        sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('', DISCOVERY_PORT))
            sock.settimeout(0.15)

            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    data, _addr = sock.recvfrom(4096)
                    msg = json.loads(data.decode('utf-8'))
                    if msg.get('action') == 'server_announce':
                        ip = msg.get('ip', '')
                        if not ip:
                            continue
                        cnt   = msg.get('user_count', 0)
                        nicks = msg.get('user_nicks', [])
                        if ip not in found:
                            found[ip] = {
                                'ip':          ip,
                                'port':        msg.get('port', 5000),
                                'host_nick':   msg.get('host_nick', '?'),
                                'server_name': msg.get('server_name', 'InPulse Server'),
                                'user_count':  cnt,
                                'user_nicks':  nicks,
                            }
                            print(f"[Discovery] Найден сервер: {ip} "
                                  f"({msg.get('server_name', '?')}, "
                                  f"{cnt} чел.)")
                        else:
                            found[ip]['user_count'] = cnt
                            found[ip]['user_nicks']  = nicks
                except socket.timeout:
                    pass
                except json.JSONDecodeError:
                    continue

        except OSError as e:
            print(f"[Discovery] discover_all bind error (порт {DISCOVERY_PORT} занят?): {e}")
        except Exception as e:
            print(f"[Discovery] discover_all error: {e}")
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

        return list(found.values())

    def discover_async(
        self,
        on_found:     callable,
        on_not_found: callable,
        timeout:      float = DISCOVERY_TIMEOUT,
    ) -> threading.Thread:
        def _run():
            result = self.discover(timeout)
            if result:
                on_found(result)
            else:
                on_not_found()

        t = threading.Thread(target=_run, daemon=True, name="discovery-search")
        t.start()
        return t

    def discover_all_async(
        self,
        on_done:  callable,
        timeout:  float = DISCOVERY_TIMEOUT,
    ) -> threading.Thread:

        def _run():
            results = self.discover_all(timeout)
            on_done(results)

        t = threading.Thread(target=_run, daemon=True, name="discovery-search-all")
        t.start()
        return t