// main.go — InPulse Pion SFU sidecar v1.1
//
// Запуск: sidecar.exe [--port 7788]
//
// При старте печатает в stdout (Python читает построчно):
//   {"event":"READY","port":7788}
//
// После этого слушает HTTP на 0.0.0.0:<port> (для стримеров в RadminVPN LAN).
//
// ── Исправления v1.1 ────────────────────────────────────────────────────────
//
//   FIX #31: http.Server с таймаутами (ReadTimeout, WriteTimeout, IdleTimeout).
//     Без них slow-loris атака или зависший клиент могли удерживать
//     соединение бесконечно, постепенно исчерпывая goroutines.
//     В LAN-режиме (RadminVPN) это не атака, а может быть просто зависший
//     клиент или временный сетевой глитч — таймауты нужны всё равно.

package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

var (
	Version = "1.1.0"
)

func main() {
	port := flag.Int("port", 7788, "HTTP port for SFU signaling API")
	flag.Parse()

	sfu := NewSFU()
	api := NewAPI(sfu)

	addr := fmt.Sprintf("0.0.0.0:%d", *port)

	readyMsg, _ := json.Marshal(map[string]interface{}{
		"event":   "READY",
		"version": Version,
		"port":    *port,
	})
	fmt.Fprintf(os.Stdout, "%s\n", readyMsg)

	log.Printf("[SFU] InPulse Pion SFU v%s слушает %s", Version, addr)

	// FIX #31: таймауты защищают от зависших соединений.
	//
	// ReadTimeout: 30 сек — запрос с SDP (может быть до 16 KB) должен
	//   прочитаться целиком за это время.
	// WriteTimeout: 30 сек — ответ с SDP тоже должен уложиться.
	// IdleTimeout: 90 сек — для Keep-Alive соединений.
	// ReadHeaderTimeout: 10 сек — защита от slow-loris на заголовках.
	srv := &http.Server{
		Addr:              addr,
		Handler:           api.Handler(),
		ReadTimeout:       30 * time.Second,
		ReadHeaderTimeout: 10 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       90 * time.Second,
	}

	// Graceful shutdown при получении SIGINT/SIGTERM.
	// Позволяет завершить текущие запросы до выхода.
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		<-sigCh
		log.Printf("[SFU] получен сигнал завершения — graceful shutdown")
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := srv.Shutdown(ctx); err != nil {
			log.Printf("[SFU] shutdown error: %v", err)
		}
		sfu.Close()
		os.Exit(0)
	}()

	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("[SFU] HTTP server error: %v", err)
	}
}
