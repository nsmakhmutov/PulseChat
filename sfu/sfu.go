// sfu.go — Ядро Pion SFU: relay треков от стримера к зрителям
//
// v4 — Per-viewer fan-out с изоляцией зрителей
//
// ── Что изменилось и почему ──────────────────────────────────────────────────
//
//   ПРОБЛЕМА v3: TrackLocalStaticRTP.Write() итерирует всех viewer-биндингов
//   последовательно в одном горутине. Если у зрителя A высокий RTT и его
//   DTLS/ICE буфер заполнен, Write() блокируется → зрители B, C, D не
//   получают пакеты пока A не "разгрузится". На 7 зрителях с разным RTT
//   это вызывает фризы у быстрых зрителей из-за медленного.
//
//   РЕШЕНИЕ: broadcasterTrack
//     1. Каждый зритель получает СВОЙ TrackLocalStaticRTP (не общий).
//     2. Для каждого зрителя запускается отдельный горутин viewerWriter,
//        который читает из персонального канала и пишет в свой трек.
//     3. relayBroadcast() читает RTP от стримера и рассылает копию
//        пакета в каналы зрителей через неблокирующий select.
//     4. Если канал зрителя переполнен (viewer lag) — пакет дропается
//        ТОЛЬКО для него. Остальные не затронуты.
//
//   БУФЕР: viewerPktBuf = 1024 пакетов ≈ 1.2 сек при 6 Мбит/с (900 байт/пкт).
//   Зрителю даётся 1.2 сек на "наверстание" прежде чем начнутся дропы.

package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"math"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/pion/interceptor"
	"github.com/pion/interceptor/pkg/intervalpli"
	"github.com/pion/interceptor/pkg/nack"
	"github.com/pion/rtcp"
	"github.com/pion/webrtc/v3"
)

// viewerPktBuf — размер канала пакетов на одного зрителя.
// 1024 пакета × 900 байт = ~921 KB ≈ 1.2 сек при 6 Мбит/с.
const viewerPktBuf = 1024

// ─── broadcasterTrack ────────────────────────────────────────────────────────
//
// Заменяет общий TrackLocalStaticRTP.
// Хранит per-viewer каналы для изолированного fan-out.

type viewerSub struct {
	track *webrtc.TrackLocalStaticRTP
	ch    chan []byte
}

type broadcasterTrack struct {
	mu       sync.RWMutex
	subs     map[string]*viewerSub // viewerID → подписка
	codec    webrtc.RTPCodecCapability
	id       string
	streamID string
}

func newBroadcasterTrack(
	codec webrtc.RTPCodecCapability,
	id, streamID string,
) *broadcasterTrack {
	return &broadcasterTrack{
		subs:     make(map[string]*viewerSub),
		codec:    codec,
		id:       id,
		streamID: streamID,
	}
}

// subscribe создаёт персональный TrackLocalStaticRTP для зрителя,
// регистрирует его в PeerConnection и возвращает канал пакетов.
// Caller должен запустить viewerWriter() для чтения из канала.
func (b *broadcasterTrack) subscribe(
	pc *webrtc.PeerConnection,
	viewerID string,
) (*webrtc.TrackLocalStaticRTP, chan []byte, error) {
	lt, err := webrtc.NewTrackLocalStaticRTP(
		webrtc.RTPCodecCapability{
			MimeType:     b.codec.MimeType,
			ClockRate:    b.codec.ClockRate,
			Channels:     b.codec.Channels,
			SDPFmtpLine:  b.codec.SDPFmtpLine,
			RTCPFeedback: b.codec.RTCPFeedback,
		},
		b.id, b.streamID,
	)
	if err != nil {
		return nil, nil, fmt.Errorf("NewTrackLocalStaticRTP: %w", err)
	}

	if _, err := pc.AddTrack(lt); err != nil {
		return nil, nil, fmt.Errorf("AddTrack: %w", err)
	}

	ch := make(chan []byte, viewerPktBuf)

	b.mu.Lock()
	b.subs[viewerID] = &viewerSub{track: lt, ch: ch}
	b.mu.Unlock()

	return lt, ch, nil
}

// unsubscribe удаляет зрителя и закрывает его канал.
// viewerWriter() завершится автоматически когда канал закрыт.
func (b *broadcasterTrack) unsubscribe(viewerID string) {
	b.mu.Lock()
	if sub, ok := b.subs[viewerID]; ok {
		close(sub.ch)
		delete(b.subs, viewerID)
	}
	b.mu.Unlock()
}

// unsubscribeAll закрывает каналы всех зрителей (вызывается при CloseStreamer).
func (b *broadcasterTrack) unsubscribeAll() {
	b.mu.Lock()
	for id, sub := range b.subs {
		close(sub.ch)
		delete(b.subs, id)
	}
	b.mu.Unlock()
}

// broadcast рассылает пакет всем зрителям через неблокирующий select.
// Зритель с переполненным каналом получает дроп (только для него).
func (b *broadcasterTrack) broadcast(pkt []byte) {
	b.mu.RLock()
	defer b.mu.RUnlock()
	for viewerID, sub := range b.subs {
		select {
		case sub.ch <- pkt:
		default:
			// Канал зрителя переполнен — дропаем для него,
			// остальные не затронуты.
			_ = viewerID // suppress unused warning; можно логировать дропы
		}
	}
}

// viewerWriter — горутин на (viewer × track).
// Читает из персонального канала и пишет в свой TrackLocalStaticRTP.
// Блокировка write() влияет только на этого зрителя.
// Завершается автоматически когда канал закрыт (unsubscribe).
func viewerWriter(lt *webrtc.TrackLocalStaticRTP, ch <-chan []byte) {
	for pkt := range ch {
		if _, err := lt.Write(pkt); err != nil {
			if errors.Is(err, io.ErrClosedPipe) {
				// PC закрыт — выходим.
				return
			}
			// Транзиентные ошибки (ErrConnectionNotStarted, DTLS handshake,
			// ICE checking) — пропускаем пакет, продолжаем.
			// Горутин переживает DTLS-хендшейк и начнёт писать когда
			// соединение будет установлено.
			continue
		}
	}
}

// ─── SFU ─────────────────────────────────────────────────────────────────────

type SFU struct {
	mu  sync.RWMutex
	api *webrtc.API

	streamerPC      *webrtc.PeerConnection
	streamerSSRC    uint32
	audioStreamerPC *webrtc.PeerConnection

	// videoBcs / audioBcs заменяют videoTracks / audioTracks.
	// Каждый broadcasterTrack хранит per-viewer подписки.
	videoBcs []*broadcasterTrack
	audioBcs []*broadcasterTrack

	trackReadyCh chan struct{}
	trackOnce    sync.Once

	viewers map[string]*viewerConn

	lossMu    sync.RWMutex
	lossStats map[string]*viewerLossInfo
}

type viewerLossInfo struct {
	LossFraction float64
	Jitter       float64
	UpdatedAt    time.Time
}

type viewerConn struct {
	id   string
	pc   *webrtc.PeerConnection
	done chan struct{}
}

func NewSFU() *SFU {
	return &SFU{
		api:          buildWebRTCAPI(),
		viewers:      make(map[string]*viewerConn),
		trackReadyCh: make(chan struct{}),
		lossStats:    make(map[string]*viewerLossInfo),
	}
}

func buildWebRTCAPI() *webrtc.API {
	m := &webrtc.MediaEngine{}

	h264Feedback := []webrtc.RTCPFeedback{
		{Type: "goog-remb"},
		{Type: "ccm", Parameter: "fir"},
		{Type: "nack"},
		{Type: "nack", Parameter: "pli"},
	}
	for _, profile := range []string{
		"level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42001f",
		"level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
		"level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=640032",
		"level-asymmetry-allowed=1;packetization-mode=0;profile-level-id=42001f",
	} {
		_ = m.RegisterCodec(webrtc.RTPCodecParameters{
			RTPCodecCapability: webrtc.RTPCodecCapability{
				MimeType:     webrtc.MimeTypeH264,
				ClockRate:    90000,
				SDPFmtpLine:  profile,
				RTCPFeedback: h264Feedback,
			},
			PayloadType: 96,
		}, webrtc.RTPCodecTypeVideo)
	}
	_ = m.RegisterCodec(webrtc.RTPCodecParameters{
		RTPCodecCapability: webrtc.RTPCodecCapability{
			MimeType:    webrtc.MimeTypeOpus,
			ClockRate:   48000,
			Channels:    2,
			SDPFmtpLine: "minptime=10;useinbandfec=1;usedtx=1",
		},
		PayloadType: 111,
	}, webrtc.RTPCodecTypeAudio)

	ir := &interceptor.Registry{}

	// PLI по интервалу — fallback когда NACK не справляется
	if f, err := intervalpli.NewReceiverInterceptor(); err == nil {
		ir.Add(f)
	}

	// NACK Responder: буферизует исходящие пакеты, ретрансмитит по запросу
	if responder, err := nack.NewResponderInterceptor(nack.ResponderSize(512)); err != nil {
		log.Printf("[SFU] NACK responder init error: %v", err)
	} else {
		ir.Add(responder)
		log.Printf("[SFU] NACK responder: buf=512 pkt")
	}

	// NACK Generator: отслеживает дыры в seq от стримера
	if generator, err := nack.NewGeneratorInterceptor(); err != nil {
		log.Printf("[SFU] NACK generator init error: %v", err)
	} else {
		ir.Add(generator)
		log.Printf("[SFU] NACK generator: OK")
	}

	if err := webrtc.ConfigureRTCPReports(ir); err != nil {
		log.Printf("[SFU] RTCP reports error: %v", err)
	}

	return webrtc.NewAPI(
		webrtc.WithMediaEngine(m),
		webrtc.WithInterceptorRegistry(ir),
	)
}

func localPeerConfig() webrtc.Configuration {
	return webrtc.Configuration{ICEServers: []webrtc.ICEServer{}}
}

// ─── Streamer ─────────────────────────────────────────────────────────────────

func (s *SFU) SetStreamerOffer(sdpStr string) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	// Закрываем старое соединение стримера
	if s.streamerPC != nil {
		_ = s.streamerPC.Close()
		s.streamerPC = nil
	}
	// Отписываем всех зрителей от старых видео-трансляций
	for _, bc := range s.videoBcs {
		bc.unsubscribeAll()
	}
	s.videoBcs = nil
	s.trackReadyCh = make(chan struct{})
	s.trackOnce = sync.Once{}

	pc, err := s.api.NewPeerConnection(localPeerConfig())
	if err != nil {
		return "", fmt.Errorf("NewPeerConnection(streamer): %w", err)
	}
	s.streamerPC = pc

	pc.OnTrack(func(rt *webrtc.TrackRemote, _ *webrtc.RTPReceiver) {
		log.Printf("[SFU] Streamer track: %s PT=%d SSRC=%d",
			rt.Codec().MimeType, rt.PayloadType(), rt.SSRC())

		bc := newBroadcasterTrack(rt.Codec().RTPCodecCapability, rt.ID(), rt.StreamID())

		s.mu.Lock()
		if rt.Kind() == webrtc.RTPCodecTypeVideo {
			s.streamerSSRC = uint32(rt.SSRC())
			s.videoBcs = append(s.videoBcs, bc)
		} else {
			s.audioBcs = append(s.audioBcs, bc)
		}
		// Подписываем уже подключённых зрителей на новый трек
		for _, v := range s.viewers {
			lt, ch, err := bc.subscribe(v.pc, v.id)
			if err != nil {
				log.Printf("[SFU] subscribe viewer %s: %v", v.id, err)
				continue
			}
			go viewerWriter(lt, ch)
		}
		s.mu.Unlock()

		s.trackOnce.Do(func() { close(s.trackReadyCh) })
		go s.relayBroadcast(rt, bc)
	})

	pc.OnICEConnectionStateChange(func(st webrtc.ICEConnectionState) {
		log.Printf("[SFU] Streamer ICE: %s", st)
	})

	if err := pc.SetRemoteDescription(webrtc.SessionDescription{
		Type: webrtc.SDPTypeOffer, SDP: sdpStr,
	}); err != nil {
		return "", fmt.Errorf("SetRemoteDescription: %w", err)
	}
	ans, err := pc.CreateAnswer(nil)
	if err != nil {
		return "", fmt.Errorf("CreateAnswer: %w", err)
	}
	gc := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(ans); err != nil {
		return "", err
	}
	<-gc
	ld := pc.LocalDescription()
	log.Printf("[SFU] Streamer answer готов, SDP len=%d", len(ld.SDP))
	return ld.SDP, nil
}

func (s *SFU) CloseStreamer() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.streamerPC != nil {
		_ = s.streamerPC.Close()
		s.streamerPC = nil
	}
	for _, bc := range s.videoBcs {
		bc.unsubscribeAll()
	}
	s.videoBcs = nil
	s.trackReadyCh = make(chan struct{})
	s.trackOnce = sync.Once{}
}

// ─── Audio Streamer ───────────────────────────────────────────────────────────

func (s *SFU) SetAudioStreamerOffer(sdpStr string) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	if s.audioStreamerPC != nil {
		_ = s.audioStreamerPC.Close()
		s.audioStreamerPC = nil
	}
	for _, bc := range s.audioBcs {
		bc.unsubscribeAll()
	}
	s.audioBcs = nil

	pc, err := s.api.NewPeerConnection(localPeerConfig())
	if err != nil {
		return "", err
	}
	s.audioStreamerPC = pc

	pc.OnTrack(func(rt *webrtc.TrackRemote, _ *webrtc.RTPReceiver) {
		log.Printf("[SFU] Audio track: %s PT=%d", rt.Codec().MimeType, rt.PayloadType())

		bc := newBroadcasterTrack(rt.Codec().RTPCodecCapability, rt.ID(), rt.StreamID())

		s.mu.Lock()
		s.audioBcs = append(s.audioBcs, bc)
		for _, v := range s.viewers {
			lt, ch, err := bc.subscribe(v.pc, v.id)
			if err != nil {
				log.Printf("[SFU] subscribe audio viewer %s: %v", v.id, err)
				continue
			}
			go viewerWriter(lt, ch)
		}
		s.trackOnce.Do(func() { close(s.trackReadyCh) })
		s.mu.Unlock()

		go s.relayBroadcast(rt, bc)
	})

	pc.OnICEConnectionStateChange(func(st webrtc.ICEConnectionState) {
		log.Printf("[SFU] Audio ICE: %s", st)
	})

	if err := pc.SetRemoteDescription(webrtc.SessionDescription{
		Type: webrtc.SDPTypeOffer, SDP: sdpStr,
	}); err != nil {
		return "", err
	}
	ans, err := pc.CreateAnswer(nil)
	if err != nil {
		return "", err
	}
	gc := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(ans); err != nil {
		return "", err
	}
	<-gc
	ld := pc.LocalDescription()
	return ld.SDP, nil
}

func (s *SFU) CloseAudioStreamer() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.audioStreamerPC != nil {
		_ = s.audioStreamerPC.Close()
		s.audioStreamerPC = nil
	}
	for _, bc := range s.audioBcs {
		bc.unsubscribeAll()
	}
	s.audioBcs = nil
}

// ─── relayBroadcast: заменяет relayRTP ───────────────────────────────────────
//
// Читает RTP пакеты от стримера и рассылает их через broadcasterTrack.broadcast().
// Каждый зритель имеет свой буферизованный канал и свой горутин viewerWriter —
// медленный зритель не блокирует поток пакетов для остальных.
//
// Буфер 16384: на loopback нет MTU 1500 — keyframe NALU может прийти одним
// большим пакетом (8–32 KB). Меньший буфер → усечение → битый H264 → артефакт.
func (s *SFU) relayBroadcast(remote *webrtc.TrackRemote, bc *broadcasterTrack) {
	buf := make([]byte, 16384)
	for {
		n, _, err := remote.Read(buf)
		if err != nil {
			if err != io.EOF {
				log.Printf("[SFU] relay read: %v", err)
			}
			return
		}
		// Копируем пакет: каждый зритель получает независимую копию.
		// make + copy дешевле sync.Pool для пакетов ~900 байт (нет GC давления).
		pkt := make([]byte, n)
		copy(pkt, buf[:n])
		bc.broadcast(pkt)
	}
}

// ─── Viewer ───────────────────────────────────────────────────────────────────

func (s *SFU) AddViewer(viewerID, sdpStr string) (string, error) {
	if viewerID == "" {
		viewerID = uuid.NewString()
	}

	// Ждём первого трека от стримера (до 30 сек)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	select {
	case <-s.trackReadyCh:
	case <-ctx.Done():
		log.Printf("[SFU] AddViewer %s: timeout waiting for streamer track", viewerID)
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	// Закрываем старое соединение если viewer переподключается
	if old, ok := s.viewers[viewerID]; ok {
		_ = old.pc.Close()
		close(old.done)
		// Отписываем от всех трансляций
		for _, bc := range s.videoBcs {
			bc.unsubscribe(viewerID)
		}
		for _, bc := range s.audioBcs {
			bc.unsubscribe(viewerID)
		}
		delete(s.viewers, viewerID)
	}

	pc, err := s.api.NewPeerConnection(localPeerConfig())
	if err != nil {
		return "", err
	}
	conn := &viewerConn{id: viewerID, pc: pc, done: make(chan struct{})}
	s.viewers[viewerID] = conn

	// Подписываем зрителя на все активные трансляции.
	// Для каждой подписки создаётся персональный TrackLocalStaticRTP
	// и запускается goroutine viewerWriter.
	for _, bc := range s.videoBcs {
		lt, ch, err := bc.subscribe(pc, viewerID)
		if err != nil {
			log.Printf("[SFU] video subscribe %s: %v", viewerID, err)
			continue
		}
		go viewerWriter(lt, ch)
	}
	for _, bc := range s.audioBcs {
		lt, ch, err := bc.subscribe(pc, viewerID)
		if err != nil {
			log.Printf("[SFU] audio subscribe %s: %v", viewerID, err)
			continue
		}
		go viewerWriter(lt, ch)
	}

	pc.OnICEConnectionStateChange(func(st webrtc.ICEConnectionState) {
		log.Printf("[SFU] Viewer %s ICE: %s", viewerID, st)
		switch st {
		case webrtc.ICEConnectionStateConnected:
			// Форсируем IDR 3 раза с паузой — зритель получит чистый первый кадр
			go func() {
				for i := 1; i <= 3; i++ {
					if i > 1 {
						time.Sleep(300 * time.Millisecond)
					}
					s.mu.RLock()
					spc := s.streamerPC
					ssrc := s.streamerSSRC
					s.mu.RUnlock()
					if spc == nil || ssrc == 0 {
						return
					}
					_ = spc.WriteRTCP([]rtcp.Packet{
						&rtcp.PictureLossIndication{MediaSSRC: ssrc},
					})
				}
			}()
			go s.readViewerRTCP(viewerID, pc)

		case webrtc.ICEConnectionStateDisconnected,
			webrtc.ICEConnectionStateFailed,
			webrtc.ICEConnectionStateClosed:
			s.RemoveViewer(viewerID)
		}
	})

	if err := pc.SetRemoteDescription(webrtc.SessionDescription{
		Type: webrtc.SDPTypeOffer, SDP: sdpStr,
	}); err != nil {
		pc.Close()
		delete(s.viewers, viewerID)
		return "", err
	}
	ans, err := pc.CreateAnswer(nil)
	if err != nil {
		pc.Close()
		delete(s.viewers, viewerID)
		return "", err
	}
	gc := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(ans); err != nil {
		pc.Close()
		delete(s.viewers, viewerID)
		return "", err
	}
	<-gc
	ld := pc.LocalDescription()
	return stripMDNSCandidates(ld.SDP), nil
}

func (s *SFU) RemoveViewer(id string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.removeViewer(id)
}

func (s *SFU) removeViewer(id string) {
	v, ok := s.viewers[id]
	if !ok {
		return
	}
	// Отписываем от всех трансляций — закрывает каналы, viewerWriter завершится
	for _, bc := range s.videoBcs {
		bc.unsubscribe(id)
	}
	for _, bc := range s.audioBcs {
		bc.unsubscribe(id)
	}
	_ = v.pc.Close()
	select {
	case <-v.done:
	default:
		close(v.done)
	}
	delete(s.viewers, id)

	s.lossMu.Lock()
	delete(s.lossStats, id)
	s.lossMu.Unlock()
}

// readViewerRTCP читает RTCP от зрителя (Receiver Reports для ABR-статистики).
// NACK перехватывается nack.ResponderInterceptor раньше и сюда не доходит.
func (s *SFU) readViewerRTCP(viewerID string, pc *webrtc.PeerConnection) {
	for _, recv := range pc.GetReceivers() {
		go func(r *webrtc.RTPReceiver) {
			for {
				pkts, _, err := r.ReadRTCP()
				if err != nil {
					return
				}
				for _, pkt := range pkts {
					if rr, ok := pkt.(*rtcp.ReceiverReport); ok {
						for _, rep := range rr.Reports {
							s.lossMu.Lock()
							s.lossStats[viewerID] = &viewerLossInfo{
								LossFraction: float64(rep.FractionLost) / 256.0,
								Jitter:       float64(rep.Jitter) / 90.0,
								UpdatedAt:    time.Now(),
							}
							s.lossMu.Unlock()
						}
					}
				}
			}
		}(recv)
	}
}

// ─── Stats ────────────────────────────────────────────────────────────────────

type AggLossStats struct {
	AvgLossPct float64 `json:"avg_loss_pct"`
	MaxLossPct float64 `json:"max_loss_pct"`
	AvgJitter  float64 `json:"avg_jitter_ms"`
	Viewers    int     `json:"viewers"`
}

func (s *SFU) LossStats() AggLossStats {
	s.lossMu.RLock()
	defer s.lossMu.RUnlock()
	cutoff := time.Now().Add(-10 * time.Second)
	var sumL, sumJ, maxL float64
	n := 0
	for _, i := range s.lossStats {
		if i.UpdatedAt.Before(cutoff) {
			continue
		}
		lp := i.LossFraction * 100
		sumL += lp
		sumJ += i.Jitter
		if lp > maxL {
			maxL = lp
		}
		n++
	}
	if n == 0 {
		return AggLossStats{}
	}
	return AggLossStats{
		AvgLossPct: math.Round(sumL/float64(n)*100) / 100,
		MaxLossPct: math.Round(maxL*100) / 100,
		AvgJitter:  math.Round(sumJ/float64(n)*100) / 100,
		Viewers:    n,
	}
}

// ─── Status ───────────────────────────────────────────────────────────────────

type Status struct {
	Streamer      string   `json:"streamer"`
	AudioStreamer string   `json:"audio_streamer"`
	VideoBcs      int      `json:"video_broadcasters"`
	AudioBcs      int      `json:"audio_broadcasters"`
	Viewers       int      `json:"viewers"`
	ViewerIDs     []string `json:"viewer_ids"`
}

func (s *SFU) Status() Status {
	s.mu.RLock()
	defer s.mu.RUnlock()
	ss := "none"
	if s.streamerPC != nil {
		ss = "connected"
	}
	as := "none"
	if s.audioStreamerPC != nil {
		as = "connected"
	}
	ids := make([]string, 0, len(s.viewers))
	for id := range s.viewers {
		ids = append(ids, id)
	}
	return Status{
		Streamer:      ss,
		AudioStreamer: as,
		VideoBcs:      len(s.videoBcs),
		AudioBcs:      len(s.audioBcs),
		Viewers:       len(s.viewers),
		ViewerIDs:     ids,
	}
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

func stripMDNSCandidates(sdp string) string {
	sep := "\r\n"
	if !strings.Contains(sdp, "\r\n") {
		sep = "\n"
	}
	lines := strings.Split(sdp, sep)
	out := make([]string, 0, len(lines))
	for _, l := range lines {
		if strings.HasPrefix(l, "a=candidate:") && strings.Contains(l, ".local") {
			continue
		}
		out = append(out, l)
	}
	return strings.Join(out, sep)
}
