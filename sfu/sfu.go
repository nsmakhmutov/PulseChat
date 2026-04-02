// sfu.go — Ядро Pion SFU: relay треков от стримера к зрителям
// v2: ABR stats, 4096 relay buffer, RTCP Receiver Report collection

package main

import (
	"context"
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
	"github.com/pion/rtcp"
	"github.com/pion/webrtc/v3"
)

type SFU struct {
	mu sync.RWMutex
	api *webrtc.API

	streamerPC   *webrtc.PeerConnection
	streamerSSRC uint32
	videoTracks  []*webrtc.TrackLocalStaticRTP
	trackReadyCh chan struct{}
	trackOnce    sync.Once

	audioStreamerPC *webrtc.PeerConnection
	audioTracks    []*webrtc.TrackLocalStaticRTP

	viewers map[string]*viewerConn

	// ABR: loss stats от зрителей
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
	api := buildWebRTCAPI()
	return &SFU{
		api:          api,
		viewers:      make(map[string]*viewerConn),
		trackReadyCh: make(chan struct{}),
		lossStats:    make(map[string]*viewerLossInfo),
	}
}

func buildWebRTCAPI() *webrtc.API {
	m := &webrtc.MediaEngine{}
	for _, profile := range []string{
		"level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42001f",
		"level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
		"level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=640032",
		"level-asymmetry-allowed=1;packetization-mode=0;profile-level-id=42001f",
	} {
		_ = m.RegisterCodec(webrtc.RTPCodecParameters{
			RTPCodecCapability: webrtc.RTPCodecCapability{
				MimeType: webrtc.MimeTypeH264, ClockRate: 90000,
				SDPFmtpLine: profile,
			},
			PayloadType: 96,
		}, webrtc.RTPCodecTypeVideo)
	}
	_ = m.RegisterCodec(webrtc.RTPCodecParameters{
		RTPCodecCapability: webrtc.RTPCodecCapability{
			MimeType: webrtc.MimeTypeOpus, ClockRate: 48000, Channels: 2,
			SDPFmtpLine: "minptime=10;useinbandfec=1;usedtx=1",
		},
		PayloadType: 111,
	}, webrtc.RTPCodecTypeAudio)

	ir := &interceptor.Registry{}
	if f, err := intervalpli.NewReceiverInterceptor(); err == nil {
		ir.Add(f)
	}
	_ = webrtc.RegisterDefaultInterceptors(m, ir)

	return webrtc.NewAPI(webrtc.WithMediaEngine(m), webrtc.WithInterceptorRegistry(ir))
}

func localPeerConfig() webrtc.Configuration {
	return webrtc.Configuration{ICEServers: []webrtc.ICEServer{}}
}

func (s *SFU) allRelayTracks() []*webrtc.TrackLocalStaticRTP {
	out := make([]*webrtc.TrackLocalStaticRTP, 0, len(s.videoTracks)+len(s.audioTracks))
	out = append(out, s.videoTracks...)
	out = append(out, s.audioTracks...)
	return out
}

func (s *SFU) SetStreamerOffer(sdpStr string) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.streamerPC != nil {
		_ = s.streamerPC.Close()
		s.streamerPC = nil; s.videoTracks = nil
		s.trackReadyCh = make(chan struct{}); s.trackOnce = sync.Once{}
	}
	pc, err := s.api.NewPeerConnection(localPeerConfig())
	if err != nil { return "", fmt.Errorf("NewPeerConnection(streamer): %w", err) }
	s.streamerPC = pc

	pc.OnTrack(func(rt *webrtc.TrackRemote, _ *webrtc.RTPReceiver) {
		if rt.Kind() == webrtc.RTPCodecTypeVideo {
			s.mu.Lock(); s.streamerSSRC = uint32(rt.SSRC()); s.mu.Unlock()
		}
		log.Printf("[SFU] Streamer track: %s PT=%d SSRC=%d", rt.Codec().MimeType, rt.PayloadType(), rt.SSRC())
		lt, err := webrtc.NewTrackLocalStaticRTP(rt.Codec().RTPCodecCapability, rt.ID(), rt.StreamID())
		if err != nil { log.Printf("[SFU] NewTrackLocal error: %v", err); return }
		s.mu.Lock()
		s.videoTracks = append(s.videoTracks, lt)
		for _, v := range s.viewers { v.pc.AddTrack(lt) }
		s.mu.Unlock()
		s.trackOnce.Do(func() { close(s.trackReadyCh) })
		go s.relayRTP(rt, lt)
	})
	pc.OnICEConnectionStateChange(func(st webrtc.ICEConnectionState) { log.Printf("[SFU] Streamer ICE: %s", st) })

	if err := pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeOffer, SDP: sdpStr}); err != nil {
		return "", fmt.Errorf("SetRemoteDescription: %w", err)
	}
	ans, err := pc.CreateAnswer(nil)
	if err != nil { return "", fmt.Errorf("CreateAnswer: %w", err) }
	gc := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(ans); err != nil { return "", err }
	<-gc
	ld := pc.LocalDescription()
	log.Printf("[SFU] Streamer answer готов, SDP len=%d", len(ld.SDP))
	return ld.SDP, nil
}

func (s *SFU) CloseStreamer() {
	s.mu.Lock(); defer s.mu.Unlock()
	if s.streamerPC != nil {
		_ = s.streamerPC.Close(); s.streamerPC = nil; s.videoTracks = nil
		s.trackReadyCh = make(chan struct{}); s.trackOnce = sync.Once{}
	}
}

func (s *SFU) SetAudioStreamerOffer(sdpStr string) (string, error) {
	s.mu.Lock(); defer s.mu.Unlock()
	if s.audioStreamerPC != nil { _ = s.audioStreamerPC.Close(); s.audioStreamerPC = nil; s.audioTracks = nil }
	pc, err := s.api.NewPeerConnection(localPeerConfig())
	if err != nil { return "", err }
	s.audioStreamerPC = pc

	pc.OnTrack(func(rt *webrtc.TrackRemote, _ *webrtc.RTPReceiver) {
		log.Printf("[SFU] Audio track: %s PT=%d", rt.Codec().MimeType, rt.PayloadType())
		lt, err := webrtc.NewTrackLocalStaticRTP(rt.Codec().RTPCodecCapability, rt.ID(), rt.StreamID())
		if err != nil { return }
		s.mu.Lock()
		s.audioTracks = append(s.audioTracks, lt)
		for _, v := range s.viewers { v.pc.AddTrack(lt) }
		s.trackOnce.Do(func() { close(s.trackReadyCh) })
		s.mu.Unlock()
		go s.relayRTP(rt, lt)
	})
	pc.OnICEConnectionStateChange(func(st webrtc.ICEConnectionState) { log.Printf("[SFU] Audio ICE: %s", st) })

	if err := pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeOffer, SDP: sdpStr}); err != nil { return "", err }
	ans, err := pc.CreateAnswer(nil)
	if err != nil { return "", err }
	gc := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(ans); err != nil { return "", err }
	<-gc
	ld := pc.LocalDescription()
	return ld.SDP, nil
}

func (s *SFU) CloseAudioStreamer() {
	s.mu.Lock(); defer s.mu.Unlock()
	if s.audioStreamerPC != nil { _ = s.audioStreamerPC.Close(); s.audioStreamerPC = nil; s.audioTracks = nil }
}

// relayRTP — буфер 4096 (FIX: было 1600, могло обрезать jumbo frames на localhost)
func (s *SFU) relayRTP(remote *webrtc.TrackRemote, local *webrtc.TrackLocalStaticRTP) {
	buf := make([]byte, 4096)
	for {
		n, _, err := remote.Read(buf)
		if err != nil { if err != io.EOF { log.Printf("[SFU] relay read: %v", err) }; return }
		if _, err := local.Write(buf[:n]); err != nil && err != io.ErrClosedPipe {
			log.Printf("[SFU] relay write: %v", err)
		}
	}
}

func (s *SFU) AddViewer(viewerID, sdpStr string) (string, error) {
	if viewerID == "" { viewerID = uuid.NewString() }
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	select { case <-s.trackReadyCh: case <-ctx.Done(): }

	s.mu.Lock(); defer s.mu.Unlock()
	if old, ok := s.viewers[viewerID]; ok { _ = old.pc.Close(); close(old.done); delete(s.viewers, viewerID) }

	pc, err := s.api.NewPeerConnection(localPeerConfig())
	if err != nil { return "", err }
	conn := &viewerConn{id: viewerID, pc: pc, done: make(chan struct{})}
	s.viewers[viewerID] = conn

	for _, lt := range s.allRelayTracks() { pc.AddTrack(lt) }

	pc.OnICEConnectionStateChange(func(st webrtc.ICEConnectionState) {
		log.Printf("[SFU] Viewer %s ICE: %s", viewerID, st)
		switch st {
		case webrtc.ICEConnectionStateConnected:
			go func() {
				for i := 1; i <= 3; i++ {
					if i > 1 { time.Sleep(300 * time.Millisecond) }
					s.mu.RLock(); spc := s.streamerPC; ssrc := s.streamerSSRC; s.mu.RUnlock()
					if spc == nil || ssrc == 0 { return }
					spc.WriteRTCP([]rtcp.Packet{&rtcp.PictureLossIndication{MediaSSRC: ssrc}})
				}
			}()
			go s.readViewerRTCP(viewerID, pc)
		case webrtc.ICEConnectionStateDisconnected, webrtc.ICEConnectionStateFailed, webrtc.ICEConnectionStateClosed:
			s.removeViewer(viewerID)
			s.lossMu.Lock(); delete(s.lossStats, viewerID); s.lossMu.Unlock()
		}
	})

	if err := pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeOffer, SDP: sdpStr}); err != nil {
		pc.Close(); delete(s.viewers, viewerID); return "", err
	}
	ans, err := pc.CreateAnswer(nil)
	if err != nil { pc.Close(); delete(s.viewers, viewerID); return "", err }
	gc := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(ans); err != nil { pc.Close(); delete(s.viewers, viewerID); return "", err }
	<-gc
	ld := pc.LocalDescription()
	return stripMDNSCandidates(ld.SDP), nil
}

func (s *SFU) readViewerRTCP(viewerID string, pc *webrtc.PeerConnection) {
	for _, recv := range pc.GetReceivers() {
		go func(r *webrtc.RTPReceiver) {
			for {
				pkts, _, err := r.ReadRTCP()
				if err != nil { return }
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

func (s *SFU) RemoveViewer(id string) { s.mu.Lock(); defer s.mu.Unlock(); s.removeViewer(id) }
func (s *SFU) removeViewer(id string) {
	if v, ok := s.viewers[id]; ok {
		_ = v.pc.Close()
		select { case <-v.done: default: close(v.done) }
		delete(s.viewers, id)
	}
}

type AggLossStats struct {
	AvgLossPct float64 `json:"avg_loss_pct"`
	MaxLossPct float64 `json:"max_loss_pct"`
	AvgJitter  float64 `json:"avg_jitter_ms"`
	Viewers    int     `json:"viewers"`
}

func (s *SFU) LossStats() AggLossStats {
	s.lossMu.RLock(); defer s.lossMu.RUnlock()
	cutoff := time.Now().Add(-10 * time.Second)
	var sumL, sumJ, maxL float64; n := 0
	for _, i := range s.lossStats {
		if i.UpdatedAt.Before(cutoff) { continue }
		lp := i.LossFraction * 100; sumL += lp; sumJ += i.Jitter
		if lp > maxL { maxL = lp }; n++
	}
	if n == 0 { return AggLossStats{} }
	return AggLossStats{
		AvgLossPct: math.Round(sumL/float64(n)*100) / 100,
		MaxLossPct: math.Round(maxL*100) / 100,
		AvgJitter:  math.Round(sumJ/float64(n)*100) / 100,
		Viewers:    n,
	}
}

func stripMDNSCandidates(sdp string) string {
	sep := "\r\n"; if !strings.Contains(sdp, "\r\n") { sep = "\n" }
	lines := strings.Split(sdp, sep)
	out := make([]string, 0, len(lines))
	for _, l := range lines {
		if strings.HasPrefix(l, "a=candidate:") && strings.Contains(l, ".local") { continue }
		out = append(out, l)
	}
	return strings.Join(out, sep)
}

type Status struct {
	Streamer      string   `json:"streamer"`
	AudioStreamer string   `json:"audio_streamer"`
	VideoTracks   int      `json:"video_tracks"`
	AudioTracks   int      `json:"audio_tracks"`
	Viewers       int      `json:"viewers"`
	ViewerIDs     []string `json:"viewer_ids"`
}

func (s *SFU) Status() Status {
	s.mu.RLock(); defer s.mu.RUnlock()
	ss := "none"; if s.streamerPC != nil { ss = "connected" }
	as := "none"; if s.audioStreamerPC != nil { as = "connected" }
	ids := make([]string, 0, len(s.viewers))
	for id := range s.viewers { ids = append(ids, id) }
	return Status{Streamer: ss, AudioStreamer: as, VideoTracks: len(s.videoTracks), AudioTracks: len(s.audioTracks), Viewers: len(s.viewers), ViewerIDs: ids}
}
