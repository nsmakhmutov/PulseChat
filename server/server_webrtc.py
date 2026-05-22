import json
import threading
from typing import Optional

from config import CMD_WEBRTC_OFFER, CMD_WEBRTC_ANSWER, SFU_PORT

def _strip_mdns_candidates(sdp: str) -> str:
    sep = "\r\n" if "\r\n" in sdp else "\n"
    lines = sdp.split(sep)
    filtered = [
        line for line in lines
        if not (line.startswith("a=candidate:") and ".local" in line)
    ]
    cleaned = sep.join(filtered)
    removed = len(lines) - len(filtered)
    if removed:
        print(f"[SFU-Proxy] _strip_mdns_candidates: убрано {removed} mDNS кандидат(ов)")
    return cleaned


def _is_local_ip(ip: str) -> bool:
    return ip in ('127.0.0.1', '::1', '', 'localhost')

_conn_send_locks: "dict[int, threading.Lock]" = {}
_conn_send_locks_guard = threading.Lock()


def _get_conn_lock(conn) -> threading.Lock:
    key = id(conn)
    with _conn_send_locks_guard:
        lk = _conn_send_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _conn_send_locks[key] = lk
        return lk


def _drop_conn_lock(conn) -> None:
    key = id(conn)
    with _conn_send_locks_guard:
        _conn_send_locks.pop(key, None)


class PionSfuProxy:

    def __init__(self, sfu_bridge=None):
        self._sfu = sfu_bridge
        self._lock = threading.Lock()
        self._viewer_conns: dict[int, object] = {}
        self._viewer_streamer_ip: dict[int, str] = {}
        self._viewer_streamer_sfu_port: dict[int, int] = {}

    @staticmethod
    def _send(conn, msg: dict) -> None:
        try:
            payload = json.dumps(msg).encode('utf-8')
        except Exception as e:
            print(f"[SFU-Proxy] _send encode: {e}")
            return

        lock = _get_conn_lock(conn)
        with lock:
            try:
                conn.sendall(payload)
            except Exception as e:
                print(f"[SFU-Proxy] send error: {e}")

    def call_async(self, coro_or_whatever):
        pass

    def trigger_viewer_connect(
        self,
        viewer_uid: int,
        streamer_uid: int,
        conn,
        quality: str = 'hq',
        streamer_ip: str = '127.0.0.1',
        streamer_sfu_port: int = SFU_PORT,
    ) -> None:
        is_local_streamer = _is_local_ip(streamer_ip)

        if is_local_streamer:
            if self._sfu is None:
                print(
                    f"[SFU-Proxy] ❌ локальный SFU не инициализирован — "
                    f"не можем подключить viewer {viewer_uid}"
                )
                return
            if not self._sfu.is_running():
                print(f"[SFU-Proxy] SFU не запущен — пробуем restart для viewer {viewer_uid}...")
                ok = self._sfu.start()
                if not ok:
                    print(
                        f"[SFU-Proxy] ❌ restart SFU не удался — "
                        f"не можем подключить viewer {viewer_uid} к локальному стримеру"
                    )
                    return
                print(f"[SFU-Proxy] ✅ SFU перезапущен, продолжаем для viewer {viewer_uid}")

        with self._lock:
            self._viewer_conns[viewer_uid] = conn
            self._viewer_streamer_ip[viewer_uid] = streamer_ip
            self._viewer_streamer_sfu_port[viewer_uid] = streamer_sfu_port

        print(
            f"[SFU-Proxy] trigger_viewer_connect: viewer={viewer_uid}, "
            f"streamer={streamer_uid}, quality={quality}, "
            f"streamer_ip={streamer_ip} ({'local' if is_local_streamer else 'remote'})"
        )
        self._send(conn, {
            'action':       CMD_WEBRTC_OFFER,
            'role':         'viewer',
            'streamer_uid': streamer_uid,
            'sdp':          '',
            'type':         'offer',
        })

    def handle_viewer_offer(
        self,
        viewer_uid: int,
        sdp: str,
        conn,
    ) -> None:
        print(f"[SFU-Proxy] handle_viewer_offer: viewer={viewer_uid}, sdp_len={len(sdp) if sdp else 0}")

        if not sdp:
            print(f"[SFU-Proxy] ❌ пустой SDP от viewer={viewer_uid}")
            return

        streamer_ip = self._viewer_streamer_ip.get(viewer_uid, '127.0.0.1')
        is_local    = _is_local_ip(streamer_ip)

        try:
            if is_local:
                if self._sfu is None:
                    print(f"[SFU-Proxy] ❌ _sfu is None — SfuBridge не инициализирован")
                    self._send(conn, {'action': 'error', 'message': 'SFU not initialized'})
                    return

                if not self._sfu.is_running():
                    print(f"[SFU-Proxy] ❌ локальный SFU не запущен (is_running=False)")
                    self._send(conn, {'action': 'error', 'message': 'SFU not running'})
                    return

                print(f"[SFU-Proxy] viewer={viewer_uid}: → POST /viewer/{viewer_uid}/offer к localhost SFU...")
                answer_sdp = self._sfu.post_viewer_offer(str(viewer_uid), sdp)

            else:
                import urllib.request
                import json as _json
                sfu_port = self._viewer_streamer_sfu_port.get(viewer_uid, SFU_PORT)
                url  = f"http://{streamer_ip}:{sfu_port}/viewer/{viewer_uid}/offer"
                body = _json.dumps({'sdp': sdp}).encode('utf-8')
                req  = urllib.request.Request(
                    url, data=body,
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                print(f"[SFU-Proxy] viewer={viewer_uid}: → remote SFU {url}")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = _json.loads(resp.read())
                answer_sdp = data.get('sdp', '')
                if not answer_sdp:
                    raise RuntimeError(f"Remote SFU ({streamer_ip}) вернул пустой SDP")

            print(f"[SFU-Proxy] viewer={viewer_uid}: ✅ SFU answer получен, len={len(answer_sdp)}")

        except Exception as e:
            print(f"[SFU-Proxy] ❌ post_viewer_offer viewer={viewer_uid}: {e}")
            import traceback; print(traceback.format_exc())
            return

        answer_sdp = _strip_mdns_candidates(answer_sdp)

        print(f"[SFU-Proxy] viewer={viewer_uid}: → CMD_WEBRTC_ANSWER (len={len(answer_sdp)})")
        self._send(conn, {
            'action': CMD_WEBRTC_ANSWER,
            'sdp':    answer_sdp,
            'type':   'answer',
        })
        print(f"[SFU-Proxy] viewer={viewer_uid}: ✅ answer отправлен")

    def close_viewer(self, viewer_uid: int) -> None:
        with self._lock:
            streamer_ip = self._viewer_streamer_ip.pop(viewer_uid, '127.0.0.1')
            old_conn = self._viewer_conns.pop(viewer_uid, None)
            self._viewer_streamer_sfu_port.pop(viewer_uid, None)

        if old_conn is not None:
            _drop_conn_lock(old_conn)

        if _is_local_ip(streamer_ip) and self._sfu is not None and self._sfu.is_running():
            self._sfu.delete_viewer(str(viewer_uid))
            print(f"[SFU-Proxy] close_viewer: viewer={viewer_uid}")

    def close_all_viewers(self) -> None:
        with self._lock:
            uids = list(self._viewer_conns.keys())
        for uid in uids:
            self.close_viewer(uid)

    def close_streamer(self, streamer_uid: int) -> None:
        pass

    def status(self) -> dict:
        if self._sfu is not None and self._sfu.is_running():
            return self._sfu.status()
        return {"streamer": "none", "viewers": 0}

    def shutdown(self) -> None:
        self.close_all_viewers()
        if self._sfu is not None and self._sfu.is_running():
            try:
                self._sfu.delete_streamer()
            except Exception as e:
                print(f"[SFU-Proxy] delete_streamer error: {e}")
            try:
                self._sfu.delete_audio_streamer()
            except Exception:
                pass
            self._sfu.stop()
        print("[SFU-Proxy] shutdown complete")
