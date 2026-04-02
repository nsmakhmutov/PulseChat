// api.go — HTTP-обработчики сигнализации SFU
//
// ИЗМЕНЕНИЯ v2:
//   Добавлен GET /stats/loss — возвращает агрегированную статистику потерь
//   для ABR (Adaptive Bitrate). Python опрашивает этот эндпоинт каждые 3 сек.

package main

import (
	"encoding/json"
	"log"
	"net/http"
	"strings"
)

type API struct {
	sfu *SFU
}

func NewAPI(sfu *SFU) *API {
	return &API{sfu: sfu}
}

func (a *API) Handler() http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("/streamer/offer", a.handleStreamerOffer)
	mux.HandleFunc("/streamer", a.handleStreamer)

	mux.HandleFunc("/streamer/audio/offer", a.handleAudioStreamerOffer)
	mux.HandleFunc("/streamer/audio", a.handleAudioStreamer)

	mux.HandleFunc("/viewer/", a.handleViewer)

	mux.HandleFunc("/health", a.handleHealth)
	mux.HandleFunc("/status", a.handleStatus)

	// ── ABR: статистика потерь для адаптивного битрейта ─────────────────
	mux.HandleFunc("/stats/loss", a.handleLossStats)

	return corsMiddleware(mux)
}

// ─── Video Streamer handlers ──────────────────────────────────────────────────

func (a *API) handleStreamerOffer(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method Not Allowed", http.StatusMethodNotAllowed)
		return
	}

	var req struct {
		SDP string `json:"sdp"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Bad Request: "+err.Error(), http.StatusBadRequest)
		return
	}
	if req.SDP == "" {
		http.Error(w, "Bad Request: sdp is empty", http.StatusBadRequest)
		return
	}

	log.Printf("[API] POST /streamer/offer (SDP len=%d)", len(req.SDP))

	answerSDP, err := a.sfu.SetStreamerOffer(req.SDP)
	if err != nil {
		log.Printf("[API] SetStreamerOffer error: %v", err)
		http.Error(w, "Internal Server Error: "+err.Error(), http.StatusInternalServerError)
		return
	}

	writeJSON(w, http.StatusOK, map[string]string{"sdp": answerSDP})
}

func (a *API) handleStreamer(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodDelete {
		http.Error(w, "Method Not Allowed", http.StatusMethodNotAllowed)
		return
	}
	log.Printf("[API] DELETE /streamer")
	a.sfu.CloseStreamer()
	w.WriteHeader(http.StatusNoContent)
}

// ─── Audio Streamer handlers ─────────────────────────────────────────────────

func (a *API) handleAudioStreamerOffer(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method Not Allowed", http.StatusMethodNotAllowed)
		return
	}

	var req struct {
		SDP string `json:"sdp"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Bad Request: "+err.Error(), http.StatusBadRequest)
		return
	}
	if req.SDP == "" {
		http.Error(w, "Bad Request: sdp is empty", http.StatusBadRequest)
		return
	}

	log.Printf("[API] POST /streamer/audio/offer (SDP len=%d)", len(req.SDP))

	answerSDP, err := a.sfu.SetAudioStreamerOffer(req.SDP)
	if err != nil {
		log.Printf("[API] SetAudioStreamerOffer error: %v", err)
		http.Error(w, "Internal Server Error: "+err.Error(), http.StatusInternalServerError)
		return
	}

	writeJSON(w, http.StatusOK, map[string]string{"sdp": answerSDP})
}

func (a *API) handleAudioStreamer(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodDelete {
		http.Error(w, "Method Not Allowed", http.StatusMethodNotAllowed)
		return
	}
	log.Printf("[API] DELETE /streamer/audio")
	a.sfu.CloseAudioStreamer()
	w.WriteHeader(http.StatusNoContent)
}

// ─── Viewer handlers ──────────────────────────────────────────────────────────

func (a *API) handleViewer(w http.ResponseWriter, r *http.Request) {
	path := strings.TrimPrefix(r.URL.Path, "/viewer/")
	parts := strings.SplitN(path, "/", 2)
	viewerID := parts[0]
	if viewerID == "" {
		http.Error(w, "Bad Request: viewer id missing", http.StatusBadRequest)
		return
	}

	action := ""
	if len(parts) == 2 {
		action = parts[1]
	}

	switch {
	case r.Method == http.MethodPost && action == "offer":
		a.handleViewerOffer(w, r, viewerID)

	case r.Method == http.MethodDelete && action == "":
		log.Printf("[API] DELETE /viewer/%s", viewerID)
		a.sfu.RemoveViewer(viewerID)
		w.WriteHeader(http.StatusNoContent)

	default:
		http.Error(w, "Not Found", http.StatusNotFound)
	}
}

func (a *API) handleViewerOffer(w http.ResponseWriter, r *http.Request, viewerID string) {
	var req struct {
		SDP string `json:"sdp"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Bad Request: "+err.Error(), http.StatusBadRequest)
		return
	}
	if req.SDP == "" {
		http.Error(w, "Bad Request: sdp is empty", http.StatusBadRequest)
		return
	}

	log.Printf("[API] POST /viewer/%s/offer (SDP len=%d)", viewerID, len(req.SDP))

	answerSDP, err := a.sfu.AddViewer(viewerID, req.SDP)
	if err != nil {
		log.Printf("[API] AddViewer %s error: %v", viewerID, err)
		http.Error(w, "Internal Server Error: "+err.Error(), http.StatusInternalServerError)
		return
	}

	writeJSON(w, http.StatusOK, map[string]string{"sdp": answerSDP, "viewer_id": viewerID})
}

// ─── ABR: Loss Stats ──────────────────────────────────────────────────────────

// GET /stats/loss — агрегированная статистика потерь от RTCP Receiver Reports.
// Python ABR поток опрашивает каждые 3 секунды для адаптации битрейта.
func (a *API) handleLossStats(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method Not Allowed", http.StatusMethodNotAllowed)
		return
	}
	stats := a.sfu.LossStats()
	writeJSON(w, http.StatusOK, stats)
}

// ─── Health / Status ──────────────────────────────────────────────────────────

func (a *API) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

func (a *API) handleStatus(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, a.sfu.Status())
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

func writeJSON(w http.ResponseWriter, code int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		log.Printf("[API] writeJSON encode error: %v", err)
	}
}

func corsMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "http://127.0.0.1")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}
