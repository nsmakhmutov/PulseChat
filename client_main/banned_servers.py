# client_main/banned_servers.py
# ──────────────────────────────────────────────────────────────────────────────
# Persistent-реестр серверов, на которых текущий пользователь забанен.
#
# Зачем: если нас забанили и мы перезапустили приложение, при показе списка
# серверов хотим визуально пометить «стоп»-значком тот сервер (по IP) — чтобы
# не тыкать туда ещё раз. TTL 7 дней — на случай если нас давно разбанили,
# а сообщить об этом технически некому.
#
# Формат bans storage:
#   { "<ip>": { "marked_at": <epoch_sec>, "reason": "<str>" } }
#
# Это файл КЛИЕНТА. Не путать с bans.json сервера (там список тех кого
# забанил локальный хост — живёт в SFUServer).
# ──────────────────────────────────────────────────────────────────────────────

import json
import os
import threading
import time

from config import BANNED_SERVERS_PATH, BANNED_SERVERS_TTL_SEC


_lock = threading.Lock()


def _read_raw() -> dict:
    """Читает JSON. При любой ошибке — возвращает пустой dict (не падаем)."""
    try:
        if not os.path.exists(BANNED_SERVERS_PATH):
            return {}
        with open(BANNED_SERVERS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[BannedServers] read error: {e}")
        return {}


def _write_raw(data: dict) -> None:
    """Атомарная запись через tmp + os.replace."""
    try:
        tmp = BANNED_SERVERS_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, BANNED_SERVERS_PATH)
    except Exception as e:
        print(f"[BannedServers] write error: {e}")


def _prune_expired(data: dict) -> dict:
    """Убирает записи старше TTL. Модифицирует dict in-place, возвращает его же."""
    now = time.time()
    stale = [
        ip for ip, info in list(data.items())
        if not isinstance(info, dict)
        or (now - float(info.get('marked_at', 0))) > BANNED_SERVERS_TTL_SEC
    ]
    for ip in stale:
        data.pop(ip, None)
    return data


def mark_banned(ip: str, reason: str = '') -> None:
    """Пометить сервер забаненным. Вызывается из MainWindow при CMD_BANNED."""
    if not ip:
        return
    with _lock:
        data = _prune_expired(_read_raw())
        data[ip] = {
            'marked_at': time.time(),
            'reason':    (reason or '')[:200],
        }
        _write_raw(data)


def unmark_banned(ip: str) -> None:
    """Убрать пометку (например если пользователь сам попросил «забыть»)."""
    if not ip:
        return
    with _lock:
        data = _prune_expired(_read_raw())
        if data.pop(ip, None) is not None:
            _write_raw(data)


def is_banned(ip: str) -> bool:
    """Быстрая проверка для рендера карточки сервера."""
    if not ip:
        return False
    with _lock:
        data = _prune_expired(_read_raw())
        return ip in data


def get_banned_set() -> set:
    """Снимок всех забаненных IP. Удобно для одного прохода по списку серверов."""
    with _lock:
        data = _prune_expired(_read_raw())
        # Сразу пишем обратно, если что-то истекло — иначе файл распухнет
        # записями-зомби. Делаем это внутри лока, без гонок.
        _write_raw(data)
        return set(data.keys())


def get_reason(ip: str) -> str:
    """Причина бана (если была сохранена). Возвращает '' если нет записи."""
    if not ip:
        return ''
    with _lock:
        data = _prune_expired(_read_raw())
        info = data.get(ip)
        if not isinstance(info, dict):
            return ''
        return str(info.get('reason', ''))
