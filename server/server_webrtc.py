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

        # ── Камера (отдельный SFU-инстанс на стороне стримера) ──────────────
        # Для камеры мы ВСЕГДА форвардим offer на SFU владельца камеры
        # (streamer_ip:camera_sfu_port), даже если владелец — это хост.
        # Локального камера-SFU у прокси нет: камера каждого юзера крутится
        # в его собственном процессе sidecar.exe на CAM_SFU_PORT.
        # Ключ — (viewer_uid, streamer_uid), т.к. зритель может смотреть
        # несколько камер одновременно.
        self._cam_viewer_conns: dict[tuple, object] = {}
        self._cam_streamer_ip: dict[tuple, str] = {}
        self._cam_streamer_port: dict[tuple, int] = {}

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

    # ─── Камера: триггер/проксирование/закрытие (всегда remote-forward) ─────
    def cam_trigger_viewer_connect(
        self,
        viewer_uid: int,
        streamer_uid: int,
        conn,
        streamer_ip: str = '127.0.0.1',
        camera_sfu_port: int = 7820,
    ) -> None:
        """Просим зрителя начать WebRTC-offer к камере streamer_uid."""
        key = (viewer_uid, streamer_uid)
        with self._lock:
            self._cam_viewer_conns[key] = conn
            self._cam_streamer_ip[key] = streamer_ip
            self._cam_streamer_port[key] = camera_sfu_port
        print(
            f"[CamProxy] trigger: viewer={viewer_uid} → cam(streamer={streamer_uid}) "
            f"@ {streamer_ip}:{camera_sfu_port}"
        )
        self._send(conn, {
            'action':       'camera_webrtc_offer',
            'role':         'viewer',
            'streamer_uid': streamer_uid,
            'sdp':          '',
            'type':         'offer',
        })

    def cam_handle_viewer_offer(
        self,
        viewer_uid: int,
        streamer_uid: int,
        sdp: str,
        conn,
    ) -> None:
        """Форвардим offer зрителя на камера-SFU владельца, шлём answer назад."""
        if not sdp:
            print(f"[CamProxy] ❌ пустой SDP viewer={viewer_uid} cam={streamer_uid}")
            return

        key = (viewer_uid, streamer_uid)
        with self._lock:
            streamer_ip = self._cam_streamer_ip.get(key, '127.0.0.1')
            cam_port    = self._cam_streamer_port.get(key, 7820)

        # viewer_id для SFU делаем уникальным на пару, чтобы один зритель мог
        # смотреть несколько камер (у каждой камеры свой SFU, но на всякий
        # случай различаем).
        sfu_viewer_id = f"{viewer_uid}-{streamer_uid}"

        try:
            import urllib.request
            import json as _json
            url  = f"http://{streamer_ip}:{cam_port}/viewer/{sfu_viewer_id}/offer"
            body = _json.dumps({'sdp': sdp}).encode('utf-8')
            req  = urllib.request.Request(
                url, data=body,
                headers={'Content-Type': 'application/json'},
                method='POST',
            )
            print(f"[CamProxy] viewer={viewer_uid}: → cam-SFU {url}")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read())
            answer_sdp = data.get('sdp', '')
            if not answer_sdp:
                raise RuntimeError(f"cam-SFU ({streamer_ip}:{cam_port}) пустой SDP")
        except Exception as e:
            print(f"[CamProxy] ❌ cam viewer offer {viewer_uid}/{streamer_uid}: {e}")
            import traceback; print(traceback.format_exc())
            return

        answer_sdp = _strip_mdns_candidates(answer_sdp)
        self._send(conn, {
            'action':       'camera_webrtc_answer',
            'streamer_uid': streamer_uid,
            'sdp':          answer_sdp,
            'type':         'answer',
        })
        print(f"[CamProxy] viewer={viewer_uid}: ✅ cam answer отправлен (cam={streamer_uid})")

    def cam_close_viewer(self, viewer_uid: int, streamer_uid: int) -> None:
        key = (viewer_uid, streamer_uid)
        with self._lock:
            streamer_ip = self._cam_streamer_ip.pop(key, '127.0.0.1')
            cam_port    = self._cam_streamer_port.pop(key, 7820)
            self._cam_viewer_conns.pop(key, None)
        sfu_viewer_id = f"{viewer_uid}-{streamer_uid}"
        try:
            import urllib.request
            url = f"http://{streamer_ip}:{cam_port}/viewer/{sfu_viewer_id}"
            req = urllib.request.Request(url, method='DELETE')
            with urllib.request.urlopen(req, timeout=5):
                pass
            print(f"[CamProxy] cam_close_viewer {viewer_uid}/{streamer_uid}")
        except Exception as e:
            print(f"[CamProxy] cam_close_viewer error {viewer_uid}/{streamer_uid}: {e}")

    def cam_close_all_for_uid(self, gone_uid: int) -> None:
        """Юзер отключился: закрываем все его камера-связи (и как зритель,
        и как стример со стороны зрителей)."""
        with self._lock:
            keys = [
                k for k in self._cam_viewer_conns
                if k[0] == gone_uid or k[1] == gone_uid
            ]
        for vk, sk in keys:
            self.cam_close_viewer(vk, sk)

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
