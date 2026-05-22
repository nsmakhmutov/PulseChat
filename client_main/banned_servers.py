
import json
import os
import threading
import time

from config import BANNED_SERVERS_PATH, BANNED_SERVERS_TTL_SEC


_lock = threading.Lock()


def _read_raw() -> dict:
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
    if not ip:
        return
    with _lock:
        data = _prune_expired(_read_raw())
        if data.pop(ip, None) is not None:
            _write_raw(data)

def is_banned(ip: str) -> bool:
    if not ip:
        return False
    with _lock:
        data = _prune_expired(_read_raw())
        return ip in data

def get_banned_set() -> set:
    with _lock:
        data = _prune_expired(_read_raw())
        _write_raw(data)
        return set(data.keys())

def get_reason(ip: str) -> str:
    if not ip:
        return ''
    with _lock:
        data = _prune_expired(_read_raw())
        info = data.get(ip)
        if not isinstance(info, dict):
            return ''
        return str(info.get('reason', ''))
