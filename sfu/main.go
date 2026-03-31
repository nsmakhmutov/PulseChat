// main.go — InPulse Pion SFU sidecar v1.0
//
// Запуск: sidecar.exe [--port 7788]
//
// При старте печатает в stdout (Python читает построчно):
//   {"event":"READY","port":7788}
//
// После этого слушает HTTP на 127.0.0.1:<port>.
//
// ── Архитектура ────────────────────────────────────────────────────────────
//
//   Rust streamer (webrtc-rs)                 aiortc viewer
//        │ WebRTC offer/answer                     │ WebRTC offer/answer
//        ▼   (через Python HTTP клиент)            ▼
//   ┌─────────────────────────────────────────────────────┐
//   │  POST /streamer/offer  → SDP answer                 │
//   │  POST /viewer/{id}/offer → SDP answer               │
//   │  DELETE /streamer / DELETE /viewer/{id}             │
//   │  GET  /health / GET /status                         │
//   │                                                     │
//   │  SFU: TrackRemote (от Rust) → TrackLocalStaticRTP   │
//   │       → WriteRTP в каждый viewer PC                 │
//   └─────────────────────────────────────────────────────┘
//
// ICE-стратегия: gather-complete (нет trickle ICE).
//   Обе стороны собирают всех кандидатов ПЕРЕД отправкой offer/answer.
//   Это даёт синхронный запрос/ответ без дополнительных round-trips.

package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
)

var (
	Version = "1.0.0"
)

func main() {
	port := flag.Int("port", 7788, "HTTP port for SFU signaling API")
	flag.Parse()

	sfu := NewSFU()
	api := NewAPI(sfu)

	// 0.0.0.0 — SFU доступен извне (для стримеров не на хост-машине, RadminVPN)
	addr := fmt.Sprintf("0.0.0.0:%d", *port)

	// Сигнал готовности для Python-хоста (читается из stdout построчно)
	readyMsg, _ := json.Marshal(map[string]interface{}{
		"event":   "READY",
		"version": Version,
		"port":    *port,
	})
	fmt.Fprintf(os.Stdout, "%s\n", readyMsg)

	log.Printf("[SFU] InPulse Pion SFU v%s слушает %s", Version, addr)

	srv := &http.Server{
		Addr:    addr,
		Handler: api.Handler(),
	}
	if err := srv.ListenAndServe(); err != nil {
		log.Fatalf("[SFU] HTTP server error: %v", err)
	}
}
