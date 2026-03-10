# server_discovery.py — UDP broadcast обнаружение сервера InPulse в RadminVPN
#
# ─── Назначение ────────────────────────────────────────────────────────────────
#
#   Позволяет клиентам автоматически находить встроенный сервер InPulse в
#   виртуальной локальной сети RadminVPN без ручного ввода IP-адреса.
#
#   ServerAnnouncer  — запускается на стороне встроенного сервера.
#                      Периодически рассылает UDP broadcast с описанием сервера.
#
#   ServerDiscovery  — запускается на стороне клиента при старте.
#                      Слушает broadcast и возвращает первый найденный сервер.
#
# ─── Протокол ──────────────────────────────────────────────────────────────────
#
#   Порт DISCOVERY_PORT (5002) — отдельный от TCP/UDP голоса.
#   Пакет: {"action": "server_announce", "ip": "26.x.x.x",
#            "port": 5000, "host_nick": "Петя"}
#
#   RadminVPN-специфика:
#     - Диапазон IP: 26.x.x.x (/8 маска виртуальной сети)
#     - Broadcast работает на уровне виртуального NIC
#     - Отправляем на 255.255.255.255 (limited broadcast) — проходит через vNIC
#     - Дополнительно пробуем 26.255.255.255 (directed broadcast в /8 сети)
#
# ───────────────────────────────────────────────────────────────────────────────

import json
import socket
import threading
import time

from config import DISCOVERY_PORT, DISCOVERY_INTERVAL, DISCOVERY_TIMEOUT

# ─── Вспомогательные функции ───────────────────────────────────────────────────

def get_local_radmin_ip() -> str:
    """
    Возвращает локальный IP-адрес RadminVPN (обычно 26.x.x.x).

    Алгоритм:
      1. Ищем адрес из диапазона 26.x.x.x среди всех NIC.
      2. Если не нашли — берём первый не-loopback IP.
      3. Если и этого нет — возвращаем '127.0.0.1'.

    Не требует сторонних библиотек (netifaces и т.п.).
    """
    try:
        hostname = socket.gethostname()
        # gethostbyname_ex возвращает (name, aliaslist, addresslist)
        all_ips = socket.gethostbyname_ex(hostname)[2]
        # Приоритет: RadminVPN 26.x.x.x
        for ip in all_ips:
            if ip.startswith('26.'):
                return ip
        # Fallback: любой не-loopback
        for ip in all_ips:
            if not ip.startswith('127.'):
                return ip
    except Exception:
        pass

    # Последний резерв: маршрут до внешнего адреса
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('26.255.255.255', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


# ─── ServerAnnouncer ───────────────────────────────────────────────────────────

class ServerAnnouncer:
    """
    Работает внутри встроенного сервера. Рассылает UDP broadcast каждые
    DISCOVERY_INTERVAL секунд, чтобы клиенты могли найти сервер автоматически.

    Отправляет на два broadcast-адреса для надёжности в RadminVPN:
      255.255.255.255  — limited broadcast (всегда работает локально)
      26.255.255.255   — directed broadcast RadminVPN /8 подсети

    Жизненный цикл:
      announcer = ServerAnnouncer("26.1.2.3", 5000, "Петя")
      announcer.start()   # запускает daemon-поток
      ...
      announcer.stop()    # останавливает поток
    """

    _BROADCAST_TARGETS = [
        ('255.255.255.255',  DISCOVERY_PORT),
        ('26.255.255.255',   DISCOVERY_PORT),
    ]

    def __init__(self, server_ip: str, server_port: int, host_nick: str):
        self.server_ip   = server_ip
        self.server_port = server_port
        self.host_nick   = host_nick
        self._running    = False
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
        print(f"[Discovery] Announcer запущен: IP={self.server_ip}, nick={self.host_nick!r}")

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

            msg = json.dumps({
                "action":    "server_announce",
                "ip":        self.server_ip,
                "port":      self.server_port,
                "host_nick": self.host_nick,
            }).encode('utf-8')

            while self._running:
                for target in self._BROADCAST_TARGETS:
                    try:
                        self._sock.sendto(msg, target)
                    except Exception as e:
                        # Один broadcast-адрес может быть недоступен — не критично
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


# ─── ServerDiscovery ───────────────────────────────────────────────────────────

class ServerDiscovery:
    """
    Слушает UDP broadcast для обнаружения серверов InPulse.

    Используется:
      1. При старте клиента (DiscoveryScreen) — ждёт DISCOVERY_TIMEOUT секунд.
      2. При авто-переключении хоста — ждёт 2-5 секунд после потери соединения.

    Возвращает:
      dict: {"ip": ..., "port": ..., "host_nick": ...} — если сервер найден.
      None: если за timeout никто не ответил.
    """

    def discover(self, timeout: float = DISCOVERY_TIMEOUT) -> dict | None:
        """
        Блокирует вызывающий поток на timeout секунд в ожидании broadcast.

        ВАЖНО: вызывать только из не-GUI потока (daemon-thread или QThread.run).
        """
        sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Привязываемся ко всем интерфейсам, включая vNIC RadminVPN
            sock.bind(('', DISCOVERY_PORT))
            sock.settimeout(timeout)

            while True:
                try:
                    data, _addr = sock.recvfrom(1024)
                    msg = json.loads(data.decode('utf-8'))
                    if msg.get('action') == 'server_announce':
                        ip   = msg.get('ip', '')
                        port = msg.get('port', 5000)
                        nick = msg.get('host_nick', '?')
                        if ip:
                            print(f"[Discovery] Сервер обнаружен: {ip} (хост: {nick!r})")
                            return {'ip': ip, 'port': port, 'host_nick': nick}
                except socket.timeout:
                    return None
                except json.JSONDecodeError:
                    continue   # мусорный пакет — игнорируем

        except OSError as e:
            # Порт занят (другой клиент/сервер на той же машине): штатная ситуация
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

    def discover_async(
        self,
        on_found: callable,
        on_not_found: callable,
        timeout: float = DISCOVERY_TIMEOUT,
    ) -> threading.Thread:
        """
        Запускает discover() в daemon-потоке и вызывает callback в конце.

        on_found(result_dict) — если сервер найден.
        on_not_found()        — если за timeout никого не обнаружено.

        Возвращает запущенный Thread (можно игнорировать).
        """
        def _run():
            result = self.discover(timeout)
            if result:
                on_found(result)
            else:
                on_not_found()

        t = threading.Thread(target=_run, daemon=True, name="discovery-search")
        t.start()
        return t