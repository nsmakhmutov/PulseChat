import json
import socket
import struct
import threading
import time

from config import DISCOVERY_PORT, DISCOVERY_INTERVAL, DISCOVERY_TIMEOUT

# ────────────────────────────────────────────────────────────────────────────
# Определение локальных адресов БЕЗ привязки к диапазону RadminVPN (26.x).
#
# Раньше приложение жёстко предпочитало IP вида 26.x — адреса RadminVPN.
# Если Radmin недоступен (упали его серверы) и друзья поднимают другой
# туннель (ZeroTier ~10.147.x, Tailscale 100.64.0.0/10, Hamachi 25.x) или
# сидят в одной физической локалке — приложение должно работать так же.
#
# Идея: не угадывать «единственно верный» IP, а собрать ВСЕ локальные
# адреса (по всем интерфейсам) и работать со списком кандидатов. Туннельные
# адреса (Radmin/Hamachi/ZeroTier/Tailscale) приоритетнее физического LAN —
# друзья обычно достижимы именно по туннелю. Но физический LAN тоже годится
# (друзья в одной комнате/сети), поэтому он не отбрасывается, а лишь ниже
# в приоритете.
#
# ВАЖНО про маршрутизацию стримов/звука/вебки: хост узнаёт реальный IP
# каждого клиента из адреса TCP-соединения (addr[0] в server.py), а не из
# того, что клиент сам о себе сообщает. Поэтому какой бы туннель клиент ни
# использовал — хост видит корректный достижимый адрес автоматически.
# Здесь мы чиним только то, что выбирает САМ хост для себя, и discovery.
# ────────────────────────────────────────────────────────────────────────────

# Префиксы «туннельных» / VPN-LAN адресов — им отдаём приоритет.
# (Radmin 26.x, Hamachi 25.x — фиксированные; ZeroTier обычно 10.147.x,
#  но настраивается; Tailscale 100.64.0.0/10 — CGNAT.)
_VPN_PREFIXES = (
    '26.',   # RadminVPN
    '25.',   # Hamachi (LogMeIn)
)


def _ip_priority(ip: str) -> int:
    """Меньше число = выше приоритет кандидата."""
    if ip.startswith('26.'):
        return 0                       # Radmin — историческое поведение, держим первым
    if ip.startswith('25.'):
        return 1                       # Hamachi
    if _is_tailscale(ip):
        return 2                       # Tailscale 100.64/10
    if ip.startswith('10.'):
        return 3                       # часто ZeroTier / корпоративный LAN
    if ip.startswith('192.168.'):
        return 5                       # домашний LAN
    if ip.startswith('172.') and _is_172_private(ip):
        return 5                       # частный LAN 172.16–31
    if ip.startswith('169.254.'):
        return 9                       # APIPA — почти бесполезно, в самый конец
    return 6                           # прочее (публичное и т.п.)


def _is_172_private(ip: str) -> bool:
    try:
        second = int(ip.split('.')[1])
        return 16 <= second <= 31
    except (IndexError, ValueError):
        return False


def _is_tailscale(ip: str) -> bool:
    # 100.64.0.0/10  →  100.64.x.x … 100.127.x.x
    if not ip.startswith('100.'):
        return False
    try:
        second = int(ip.split('.')[1])
        return 64 <= second <= 127
    except (IndexError, ValueError):
        return False


def get_all_local_ips() -> list[str]:
    """
    Все локальные IPv4-адреса машины по всем интерфейсам, без loopback,
    отсортированные по приоритету (туннели → LAN → прочее).

    Используем два независимых способа и объединяем результат, т.к. на
    Windows ни один по отдельности не покрывает все интерфейсы надёжно:
      1) gethostbyname_ex(hostname) — обычно отдаёт все адаптеры;
      2) UDP-connect трюк к нескольким «дальним» адресам — выясняет IP
         исходящего интерфейса для разных направлений (включая туннели).
    """
    ips: set[str] = set()

    # Способ 1: все адреса по hostname.
    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            if ip and not ip.startswith('127.'):
                ips.add(ip)
    except Exception:
        pass

    # Способ 2: исходящий интерфейс к разным «направлениям».
    # connect на UDP-сокете не шлёт пакетов, но заставляет ОС выбрать
    # локальный интерфейс/адрес для маршрута к цели → getsockname().
    for probe in ('26.255.255.255', '25.255.255.255',
                  '100.100.100.100', '10.255.255.255',
                  '8.8.8.8', '192.168.255.255'):
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.05)
            s.connect((probe, 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith('127.'):
                ips.add(ip)
        except Exception:
            pass
        finally:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass

    return sorted(ips, key=_ip_priority)


def get_preferred_local_ip() -> str:
    """
    Лучший единственный локальный IP для самопредставления хоста.
    Возвращает первый по приоритету кандидат; если ничего не нашли —
    '127.0.0.1' (как и раньше — деградация в loopback).
    """
    candidates = get_all_local_ips()
    if candidates:
        return candidates[0]
    return '127.0.0.1'


# ── Обратная совместимость ───────────────────────────────────────────────────
# Множество вызовов по коду использует это имя. Оставляем как тонкий алиас,
# чтобы ничего не пришлось менять в UI/сервере. Поведение теперь agnostic:
# при наличии Radmin вернёт 26.x (как раньше), иначе — лучший доступный IP.
def get_local_radmin_ip() -> str:
    return get_preferred_local_ip()


def _directed_broadcasts() -> list[str]:
    """
    Broadcast-адреса для рассылки анонсов. Для каждого локального IP
    добавляем его /24-broadcast (x.y.z.255) — это покрывает Radmin,
    Hamachi, ZeroTier, физический LAN. Плюс глобальный 255.255.255.255.

    Замечание: Tailscale — чистый L3 без L2-broadcast, туда анонсы не
    дойдут. Для Tailscale рассчитываем на ручной ввод IP хоста (в UI это
    уже есть). Остальные туннели L2-broadcast эмулируют.
    """
    targets = {'255.255.255.255'}
    for ip in get_all_local_ips():
        parts = ip.split('.')
        if len(parts) == 4:
            targets.add(f"{parts[0]}.{parts[1]}.{parts[2]}.255")
    return list(targets)


class ServerAnnouncer:

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
                # candidate_ips — список всех адресов хоста. Зритель, не
                # получивший наш пакет по своему туннелю, всё равно сможет
                # выбрать достижимый адрес из этого списка (а discover()
                # вдобавок предпочитает source-IP пакета).
                _candidates = get_all_local_ips() or [self.server_ip]
                msg = json.dumps({
                    "action":        "server_announce",
                    "ip":            self.server_ip,
                    "candidate_ips": _candidates,
                    "port":          self.server_port,
                    "host_nick":     self.host_nick[:16],
                    "server_name":   self.server_name,
                    "user_count":    self._get_count(),
                    "user_nicks":    _safe_nicks,
                }).encode('utf-8')

                # Пересчитываем broadcast-цели каждый цикл: пользователь мог
                # включить новый туннель уже после старта сервера.
                for tgt_ip in _directed_broadcasts():
                    try:
                        self._sock.sendto(msg, (tgt_ip, DISCOVERY_PORT))
                    except Exception as e:
                        print(f"[Discovery] Announce to {tgt_ip} error: {e}")
                time.sleep(DISCOVERY_INTERVAL)

        except Exception as e:
            print(f"[Discovery] Announcer fatal error: {e}")
        finally:
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass


def _pick_reachable_ip(announced_ip: str, candidate_ips, src_ip: str) -> str:
    """
    Выбирает адрес хоста, по которому зритель РЕАЛЬНО его достанет.

    Приоритет:
      1) src_ip — адрес, с которого пришёл announce-пакет: это гарантированно
         достижимый со стороны зрителя интерфейс хоста (тот же трюк, что
         сервер делает с addr[0] для клиентов). Самый надёжный сигнал.
      2) Из candidate_ips — первый, чья /24-подсеть совпадает с подсетью
         src_ip (значит, мы в одной сети по этому интерфейсу).
      3) announced_ip — то, что хост сам о себе заявил (старое поведение).
    """
    # 1) source-IP пакета — лучший вариант.
    if src_ip and not src_ip.startswith('127.'):
        return src_ip

    # 2) совпадение подсетей.
    if candidate_ips and src_ip:
        src_net = '.'.join(src_ip.split('.')[:3])
        for c in candidate_ips:
            if '.'.join(c.split('.')[:3]) == src_net:
                return c

    # 3) fallback на заявленный или первый кандидат.
    if announced_ip:
        return announced_ip
    if candidate_ips:
        return candidate_ips[0]
    return ''


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
                        src_ip = _addr[0] if _addr else ''
                        ip = _pick_reachable_ip(
                            msg.get('ip', ''),
                            msg.get('candidate_ips', []),
                            src_ip,
                        )
                        port = msg.get('port', 5000)
                        nick = msg.get('host_nick', '?')
                        name = msg.get('server_name', 'InPulse Server')
                        cnt  = msg.get('user_count', 0)
                        if ip:
                            print(f"[Discovery] Сервер обнаружен: {ip} "
                                  f"(src={src_ip}, хост: {nick!r})")
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
                        src_ip = _addr[0] if _addr else ''
                        ip = _pick_reachable_ip(
                            msg.get('ip', ''),
                            msg.get('candidate_ips', []),
                            src_ip,
                        )
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
