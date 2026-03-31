"""
server_webrtc.py — Pion SFU Proxy v3

Заменяет WebRTCSFU (aiortc на сервере) на тонкий HTTP-прокси к Go Pion SFU.

─── FIX: remote streamer viewer connect ───────────────────────────────────────
  Старый trigger_viewer_connect() проверял self._sfu.is_running() для ВСЕХ
  стримеров — включая удалённых (другая машина, другой SFU).

  Новый код: локальный SFU нужен ТОЛЬКО когда стример на той же машине.
  Для удалённых стримеров (streamer_ip != 127.0.0.1):
    - Триггер отправляем сразу, локальный SFU не нужен.
    - Offer зрителя пойдёт напрямую к remote SFU (26.x.x.x:7788).

─── Поток сигнализации ────────────────────────────────────────────────────────

  Стример (любой участник):
    Rust webrtc-rs → свой sidecar.exe (на машине стримера).
    Сервер только отслеживает is_streaming=True.

  Зритель:
    1. Зритель: stream_watch_start → сервер
    2. Сервер → зрителю: CMD_WEBRTC_OFFER role='viewer' (пустой триггер)
    3. Зритель создаёт aiortc PC (recvonly), createOffer, ICE gathering
    4. Зритель → сервер: CMD_WEBRTC_OFFER role='viewer_offer' sdp=<offer>
    5a. Стример локальный: POST /viewer/{uid}/offer → localhost SFU → answer
    5b. Стример удалённый: POST /viewer/{uid}/offer → streamer_ip:7788 → answer
    6. Сервер → зрителю: CMD_WEBRTC_ANSWER sdp=<answer>
    7. Зритель: setRemoteDescription(answer) → ICE → RTP от SFU стримера
"""

import json
import threading
from typing import Optional

from config import CMD_WEBRTC_OFFER, CMD_WEBRTC_ANSWER


def _strip_mdns_candidates(sdp: str) -> str:
    """
    Убираем mDNS *.local кандидаты из SDP answer Pion SFU.

    Rust webrtc-rs регистрирует mDNS агент (0.0.0.0:5353).
    Pion SFU включает *.local кандидаты в answer для зрителя.
    aiortc на Windows пытается резолвить их через multicast mDNS —
    это зависает навсегда (Bonjour/Avahi не установлен).
    """
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
    """Возвращает True если IP — это localhost (стример на той же машине)."""
    return ip in ('127.0.0.1', '::1', '', 'localhost')


class PionSfuProxy:
    """
    Тонкий прокси к Go Pion SFU (sidecar.exe).

    Не держит WebRTC-состояние — всё в SFU.
    Не нужен asyncio event loop — HTTP запросы синхронные (короткие, localhost).
    Потокобезопасен.
    """

    def __init__(self, sfu_bridge=None):
        """
        sfu_bridge — экземпляр SfuBridge (уже запущенный).
        Если None — режим без WebRTC (fallback).
        """
        self._sfu = sfu_bridge
        self._lock = threading.Lock()
        # viewer_uid (int) → conn (socket)
        self._viewer_conns: dict[int, object] = {}
        # viewer_uid (int) → RadminVPN IP стримера
        self._viewer_streamer_ip: dict[int, str] = {}
        # viewer_uid (int) → реальный порт SFU стримера (динамический)
        self._viewer_streamer_sfu_port: dict[int, int] = {}

    # ── Утилита отправки ─────────────────────────────────────────────────────

    @staticmethod
    def _send(conn, msg: dict) -> None:
        try:
            conn.sendall(json.dumps(msg).encode('utf-8'))
        except Exception as e:
            print(f"[SFU-Proxy] send error: {e}")

    # ── Совместимость call_async ──────────────────────────────────────────────

    def call_async(self, coro_or_whatever):
        pass  # server.py вызывает методы напрямую

    # ── Viewer connect ────────────────────────────────────────────────────────

    def trigger_viewer_connect(
        self,
        viewer_uid: int,
        streamer_uid: int,
        conn,
        quality: str = 'hq',
        streamer_ip: str = '127.0.0.1',
        streamer_sfu_port: int = 7788,
    ) -> None:
        """
        Шаг 2: посылаем зрителю пустой CMD_WEBRTC_OFFER (триггер).
        Зритель создаст offer и пришлёт нам через handle_viewer_offer.

        FIX: локальный SFU нужен ТОЛЬКО когда стример локальный.
        Для удалённых стримеров (IP не localhost) локальный SFU не участвует —
        offer зрителя пойдёт напрямую к sidecar.exe стримера по RadminVPN.
        """
        is_local_streamer = _is_local_ip(streamer_ip)

        if is_local_streamer:
            # Стример на нашей машине — нужен локальный SFU
            if self._sfu is None:
                print(
                    f"[SFU-Proxy] ❌ локальный SFU не инициализирован — "
                    f"не можем подключить viewer {viewer_uid}"
                )
                return
            if not self._sfu.is_running():
                # FIX #3: Пробуем перезапустить SFU перед тем как отказать зрителю
                print(f"[SFU-Proxy] SFU не запущен — пробуем restart для viewer {viewer_uid}...")
                ok = self._sfu.start()
                if not ok:
                    print(
                        f"[SFU-Proxy] ❌ restart SFU не удался — "
                        f"не можем подключить viewer {viewer_uid} к локальному стримеру"
                    )
                    return
                print(f"[SFU-Proxy] ✅ SFU перезапущен, продолжаем для viewer {viewer_uid}")
        # Для удалённых стримеров пропускаем проверку: локальный SFU не нужен

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
        """
        Шаг 4-6: получаем offer от зрителя → POST в SFU → answer → viewer.

        FIX: для локального стримера используем self._sfu (localhost HTTP).
             для удалённого стримера — POST напрямую к streamer_ip:7788.
        """
        print(f"[SFU-Proxy] handle_viewer_offer: viewer={viewer_uid}, sdp_len={len(sdp) if sdp else 0}")

        if not sdp:
            print(f"[SFU-Proxy] ❌ пустой SDP от viewer={viewer_uid}")
            return

        streamer_ip = self._viewer_streamer_ip.get(viewer_uid, '127.0.0.1')
        is_local    = _is_local_ip(streamer_ip)

        try:
            if is_local:
                # ── Локальный стример ────────────────────────────────────────
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
                # ── Удалённый стример (RadminVPN) ────────────────────────────
                import urllib.request
                import json as _json
                sfu_port = self._viewer_streamer_sfu_port.get(viewer_uid, 7788)
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

        # Фильтруем mDNS *.local кандидаты
        answer_sdp = _strip_mdns_candidates(answer_sdp)

        print(f"[SFU-Proxy] viewer={viewer_uid}: → CMD_WEBRTC_ANSWER (len={len(answer_sdp)})")
        self._send(conn, {
            'action': CMD_WEBRTC_ANSWER,
            'sdp':    answer_sdp,
            'type':   'answer',
        })
        print(f"[SFU-Proxy] viewer={viewer_uid}: ✅ answer отправлен")

    # ── Viewer disconnect ─────────────────────────────────────────────────────

    def close_viewer(self, viewer_uid: int) -> None:
        """DELETE /viewer/{viewer_uid} в Pion SFU (локальном)."""
        with self._lock:
            streamer_ip = self._viewer_streamer_ip.pop(viewer_uid, '127.0.0.1')
            self._viewer_conns.pop(viewer_uid, None)
            self._viewer_streamer_sfu_port.pop(viewer_uid, None)

        # Для удалённого стримера — не трогаем его SFU (он сам закроет)
        if _is_local_ip(streamer_ip) and self._sfu is not None and self._sfu.is_running():
            self._sfu.delete_viewer(str(viewer_uid))
            print(f"[SFU-Proxy] close_viewer: viewer={viewer_uid}")

    def close_all_viewers(self) -> None:
        with self._lock:
            uids = list(self._viewer_conns.keys())
        for uid in uids:
            self.close_viewer(uid)

    # ── Streamer disconnect ───────────────────────────────────────────────────

    def close_streamer(self, streamer_uid: int) -> None:
        """DELETE /streamer в Pion SFU."""
        if self._sfu is not None and self._sfu.is_running():
            self._sfu.delete_streamer()
            print(f"[SFU-Proxy] close_streamer: streamer={streamer_uid}")
        self.close_all_viewers()

    # ── Status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        if self._sfu is not None and self._sfu.is_running():
            return self._sfu.status()
        return {"streamer": "none", "viewers": 0}

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        self.close_all_viewers()
        if self._sfu is not None and self._sfu.is_running():
            self._sfu.delete_streamer()
        print("[SFU-Proxy] shutdown")