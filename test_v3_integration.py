"""
test_v3_integration.py — Smoke-тест запуска v3 компонентов

Проверяет:
  1. sidecar.exe (Pion SFU) стартует и отвечает на /health
  2. media-engine.exe стартует, шлёт READY, принимает START_STREAM
  3. SfuBridge.post_streamer_offer() доходит до SFU (возвращает SDP answer)
  4. SfuBridge.status() показывает streamer connected

Запуск:
  cd test_v2
  python test_v3_integration.py

Требования:
  - sidecar.exe рядом с этим файлом (собран из sfu/)
  - media-engine.exe рядом (собран из media-engine/)
  - Windows (WGC capture требует Windows; test падает на Linux — это нормально)
"""

import json
import os
import subprocess
import sys
import time

# ── Пути ─────────────────────────────────────────────────────────────────────
BASE = os.path.dirname(os.path.abspath(__file__))
SIDECAR_EXE = os.path.join(BASE, "sidecar.exe")
MEDIA_EXE   = os.path.join(BASE, "media-engine.exe")

PASS = "\033[32m✅\033[0m"
FAIL = "\033[31m❌\033[0m"
WARN = "\033[33m⚠️ \033[0m"

results = []

def check(name: str, ok: bool, detail: str = ""):
    sym = PASS if ok else FAIL
    print(f"  {sym} {name}" + (f": {detail}" if detail else ""))
    results.append((name, ok))
    return ok


def run_test():
    print("\n═══════════════════════════════════════════════════")
    print("  InPulse v3 Integration Smoke Test")
    print("═══════════════════════════════════════════════════\n")

    # ── 1. Проверка бинарей ───────────────────────────────────────────────────
    print("1. Бинарные файлы")
    sidecar_ok = check("sidecar.exe существует",    os.path.isfile(SIDECAR_EXE))
    media_ok   = check("media-engine.exe существует", os.path.isfile(MEDIA_EXE))

    # ── 2. Тест SfuBridge ────────────────────────────────────────────────────
    print("\n2. Go Pion SFU (SfuBridge)")
    sfu = None
    if sidecar_ok:
        try:
            from sfu_bridge import SfuBridge
            sfu = SfuBridge(on_log=lambda s: print(f"      [SFU] {s}"))
            started = sfu.start(timeout=6.0)
            check("SfuBridge.start()", started)

            if started:
                alive = sfu.health()
                check("GET /health → ok", alive)

                st = sfu.status()
                check("GET /status: streamer=none",
                      st.get("streamer") == "none", str(st))
        except Exception as e:
            check("SfuBridge import/start", False, str(e))
    else:
        print(f"  {WARN} sidecar.exe не найден — пропускаем тест SFU")

    # ── 3. Тест MediaEngineBridge ─────────────────────────────────────────────
    print("\n3. Rust Media Engine (MediaEngineBridge)")
    bridge = None
    if media_ok and sfu is not None:
        try:
            from media_engine_bridge import MediaEngineBridge
            bridge = MediaEngineBridge(
                sfu_bridge=sfu,
                on_log=lambda s: print(f"      [Media] {s}"),
                env_log_level="warn",
            )
            started = bridge.start(timeout=5.0)
            check("MediaEngineBridge.start()", started)

            if started:
                check("is_running()", bridge.is_running())
        except Exception as e:
            check("MediaEngineBridge import/start", False, str(e))
    else:
        print(f"  {WARN} media-engine.exe или SFU недоступен — пропускаем")

    # ── 4. SFU streamer offer round-trip (mock) ───────────────────────────────
    print("\n4. SFU Streamer Offer Round-Trip (mock SDP)")
    if sfu is not None and sfu.is_running():
        # Минимальный валидный SDP offer для теста (соответствует стандарту WebRTC/Pion)
        mock_sdp = (
            "v=0\r\n"
            "o=- 8765432100 2 IN IP4 127.0.0.1\r\n"
            "s=-\r\n"
            "t=0 0\r\n"
            "a=group:BUNDLE 0\r\n"
            "a=extmap-allow-mixed\r\n"
            "a=msid-semantic: WMS stream0\r\n"
            "m=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
            "c=IN IP4 0.0.0.0\r\n"
            "a=rtcp:9 IN IP4 0.0.0.0\r\n"
            "a=ice-ufrag:testufrag\r\n"
            "a=ice-pwd:testpassword0000000000000000\r\n"
            "a=ice-options:trickle\r\n"
            "a=fingerprint:sha-256 "
            "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:"
            "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99\r\n"
            "a=setup:actpass\r\n"
            "a=mid:0\r\n"
            "a=extmap:1 urn:ietf:params:rtp-hdrext:toffset\r\n"
            "a=sendonly\r\n"
            "a=msid:stream0 video0\r\n"
            "a=rtcp-mux\r\n"
            "a=rtcp-rsize\r\n"
            "a=rtpmap:96 H264/90000\r\n"
            "a=fmtp:96 level-asymmetry-allowed=1;packetization-mode=1;"
            "profile-level-id=42001f\r\n"
            "a=ssrc:1111111111 cname:test-cname\r\n"
        )
        try:
            answer = sfu.post_streamer_offer(mock_sdp)
            check("post_streamer_offer() → answer SDP",
                  bool(answer) and "v=0" in answer,
                  f"len={len(answer)}")
            st2 = sfu.status()
            check("SFU status: streamer=connected",
                  st2.get("streamer") == "connected", str(st2))
        except Exception as e:
            check("post_streamer_offer()", False, str(e))
    else:
        print(f"  {WARN} SFU не запущен — пропускаем offer тест")

    # ── Cleanup ───────────────────────────────────────────────────────────────
    print("\n5. Cleanup")
    if bridge is not None:
        try:
            bridge.stop()
            check("MediaEngineBridge.stop()", True)
        except Exception as e:
            check("MediaEngineBridge.stop()", False, str(e))

    if sfu is not None:
        try:
            sfu.stop()
            check("SfuBridge.stop()", True)
        except Exception as e:
            check("SfuBridge.stop()", False, str(e))

    # ── Итог ──────────────────────────────────────────────────────────────────
    passed = sum(1 for _, ok in results if ok)
    total  = len(results)
    print(f"\n{'═'*51}")
    print(f"  Результат: {passed}/{total} тестов прошло")
    if passed == total:
        print(f"  {PASS} Все тесты пройдены!")
    else:
        failed = [name for name, ok in results if not ok]
        print(f"  {FAIL} Провалено: {failed}")
    print(f"{'═'*51}\n")
    return passed == total


if __name__ == "__main__":
    ok = run_test()
    sys.exit(0 if ok else 1)
