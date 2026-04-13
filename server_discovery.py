# server_discovery.py — UDP broadcast обнаружение серверов InPulse в RadminVPN
#
# ─── Назначение ────────────────────────────────────────────────────────────────
#
#   Позволяет клиентам автоматически находить все встроенные серверы InPulse в
#   виртуальной локальной сети RadminVPN без ручного ввода IP-адреса.
#
#   ServerAnnouncer  — запускается на стороне встроенного сервера.
#                      Периодически рассылает UDP broadcast с описанием сервера.
#                      Теперь включает server_name и get_user_count для живого счётчика.
#
#   ServerDiscovery  — запускается на стороне клиента.
#                      discover()     — возвращает первый найденный сервер (совместимость).
#                      discover_all() — собирает ВСЕ серверы за timeout секунд.
#
# ─── Протокол ──────────────────────────────────────────────────────────────────
#
#   Порт DISCOVERY_PORT (5002) — отдельный от TCP/UDP голоса.
#   Пакет: {"action": "server_announce", "ip": "26.x.x.x",
#            "port": 5000, "host_nick": "Петя",
#            "server_name": "Сервер Пети", "user_count": 3}
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

    ИЗМЕНЕНИЯ v2:
      - Добавлены параметры server_name и get_user_count.
      - server_name  — отображаемое имя сервера в списке серверов клиента.
      - get_user_count — callable(), вызывается каждый цикл для получения
                         актуального числа участников (живой счётчик).

    Отправляет на два broadcast-адреса для надёжности в RadminVPN:
      255.255.255.255  — limited broadcast (всегда работает локально)
      26.255.255.255   — directed broadcast RadminVPN /8 подсети

    Жизненный цикл:
      announcer = ServerAnnouncer("26.1.2.3", 5000, "Петя", "Сервер Пети",
                                  get_user_count=lambda: server.get_client_count())
      announcer.start()   # запускает daemon-поток
      ...
      announcer.stop()    # останавливает поток
    """

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
        # FIX #5: callable() → список никнеймов текущих участников сервера.
        # Используется для hover-попапа в MultiServerScreen.
        # Макс 30 юзеров × ~20 байт/ник = ~600 байт — безопасно для UDP.
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
                # user_count и user_nicks запрашиваем каждый цикл — они меняются динамически
                _nicks = self._get_nicks()
                # Обрезаем каждый ник до 16 символов и ограничиваем список
                # чтобы UDP-пакет не превысил безопасный размер (~1400 байт)
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

    Методы:
      discover()      — возвращает ПЕРВЫЙ найденный сервер (обратная совместимость).
      discover_all()  — собирает ВСЕ уникальные серверы за timeout секунд.
      discover_async()— асинхронный discover() в daemon-потоке (обратная совместимость).
      discover_all_async() — асинхронный discover_all() в daemon-потоке.

    Информация о каждом сервере:
      dict: {
        "ip":          str,   — RadminVPN IP встроенного сервера
        "port":        int,   — TCP-порт (обычно 5000)
        "host_nick":   str,   — ник хоста
        "server_name": str,   — отображаемое имя сервера
        "user_count":  int,   — текущее число участников
      }
    """

    # ── discover (первый найденный — обратная совместимость) ──────────────────

    def discover(self, timeout: float = DISCOVERY_TIMEOUT) -> dict | None:
        """
        Блокирует вызывающий поток на timeout секунд в ожидании broadcast.
        Возвращает первый найденный сервер или None.

        ВАЖНО: вызывать только из не-GUI потока (daemon-thread или QThread.run).
        """
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
                    continue   # мусорный пакет — игнорируем

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

    # ── discover_all (все серверы за timeout) ─────────────────────────────────

    def discover_all(self, timeout: float = DISCOVERY_TIMEOUT) -> list[dict]:
        """
        Слушает UDP broadcast в течение timeout секунд и возвращает список
        ВСЕХ уникальных серверов, обнаруженных за это время.

        Дедупликация по IP: каждый сервер только один раз.
        При повторных пакетах от того же IP обновляем user_count.

        ВАЖНО: вызывать только из не-GUI потока (daemon-thread или QThread.run).
        Используется DiscoveryScreen для отображения списка всех серверов.
        """
        found: dict[str, dict] = {}   # ip → server_info
        sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('', DISCOVERY_PORT))
            # Короткий recv-таймаут — polling-цикл позволяет обновлять счётчики
            # уже найденных серверов до истечения общего timeout.
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
                            # Обновляем живой счётчик без пересоздания записи
                            found[ip]['user_count'] = cnt
                            found[ip]['user_nicks']  = nicks
                except socket.timeout:
                    pass   # нормально — ждём следующий пакет
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

    # ── Async-обёртки ─────────────────────────────────────────────────────────

    def discover_async(
        self,
        on_found:     callable,
        on_not_found: callable,
        timeout:      float = DISCOVERY_TIMEOUT,
    ) -> threading.Thread:
        """
        Запускает discover() в daemon-потоке и вызывает callback в конце.

        on_found(result_dict) — если сервер найден.
        on_not_found()        — если за timeout никого не обнаружено.
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

    def discover_all_async(
        self,
        on_done:  callable,
        timeout:  float = DISCOVERY_TIMEOUT,
    ) -> threading.Thread:
        """
        Запускает discover_all() в daemon-потоке.

        on_done(servers_list) — вызывается по завершении.
        servers_list — list[dict], может быть пустым.
        """
        def _run():
            results = self.discover_all(timeout)
            on_done(results)

        t = threading.Thread(target=_run, daemon=True, name="discovery-search-all")
        t.start()
        return t