// sfu.go — Ядро Pion SFU: relay треков от стримера к зрителям
//
// ── Поток данных ────────────────────────────────────────────────────────────
//
//   Streamer PC (webrtc-rs)          Audio PC (Python aiortc + DLL)
//       └── OnTrack → TrackRemote        └── OnTrack → TrackRemote
//                        │                                │
//               videoTracks relay                audioTracks relay
//                        │                                │
//             ┌──────────┼──────────┐      ┌─────────────┘
//             ▼          ▼          ▼      ▼
//         Viewer A   Viewer B   Viewer C   (aiortc, получают video+audio)
//
// ── ICE gather-complete ──────────────────────────────────────────────────────
//   При создании каждого PeerConnection ждём GatheringCompletePromise
//   и только тогда возвращаем LocalDescription (answer).
//   Нет trickle-ICE → нет отдельных /candidate эндпоинтов.
//
// ── FIX: Python Audio Streamer ───────────────────────────────────────────────
//   Добавлен audioStreamerPC — отдельное WebRTC PC для Python-стороннего
//   захвата системного звука (InPulseAudioExclusion.dll → aiortc).
//   Rust media-engine НЕ захватывает аудио (нет доступа к C++ DLL).
//   Python создаёт aiortc PC с SystemAudioTrack, делает offer,
//   POSTит на /streamer/audio/offer → SFU создаёт audio relay → зрители.

package main

import (
	"context"
	"fmt"
	"io"
	"log"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/pion/interceptor"
	"github.com/pion/interceptor/pkg/intervalpli"
	"github.com/pion/rtcp"
	"github.com/pion/webrtc/v3"
)

// ─── SFU ──────────────────────────────────────────────────────────────────────

type SFU struct {
	mu sync.RWMutex

	api *webrtc.API // общий Pion API (кодеки, интерцепторы)

	// ── Видео стример (Rust webrtc-rs) ──────────────────────────────────────
	streamerPC   *webrtc.PeerConnection
	streamerSSRC uint32                        // SSRC видео-трека (для PLI)
	videoTracks  []*webrtc.TrackLocalStaticRTP // relay-треки видео
	trackReadyCh chan struct{}                  // закрывается когда хоть один трек готов
	trackOnce    sync.Once

	// ── Аудио стример (Python aiortc + InPulseAudioExclusion.dll) ───────────
	// FIX: Rust media-engine не захватывает системный звук.
	// Python создаёт отдельный aiortc PC с SystemAudioTrack и подключает сюда.
	audioStreamerPC *webrtc.PeerConnection
	audioTracks    []*webrtc.TrackLocalStaticRTP // relay-треки аудио

	// Viewers: viewer_id → *viewerConn
	viewers map[string]*viewerConn
}

type viewerConn struct {
	id   string
	pc   *webrtc.PeerConnection
	done chan struct{} // закрывается при дисконнекте
}

// NewSFU создаёт SFU с правильно настроенным Pion API.
func NewSFU() *SFU {
	api := buildWebRTCAPI()
	return &SFU{
		api:          api,
		viewers:      make(map[string]*viewerConn),
		trackReadyCh: make(chan struct{}),
	}
}

// buildWebRTCAPI регистрирует кодеки и интерцепторы.
func buildWebRTCAPI() *webrtc.API {
	m := &webrtc.MediaEngine{}

	// H264 — приоритетный кодек стримера (NVENC/AMF output)
	for _, profile := range []string{
		"level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42001f",
		"level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
		"level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=640032",
		"level-asymmetry-allowed=1;packetization-mode=0;profile-level-id=42001f",
	} {
		if err := m.RegisterCodec(webrtc.RTPCodecParameters{
			RTPCodecCapability: webrtc.RTPCodecCapability{
				MimeType:     webrtc.MimeTypeH264,
				ClockRate:    90000,
				Channels:     0,
				SDPFmtpLine:  profile,
				RTCPFeedback: nil,
			},
			PayloadType: 96,
		}, webrtc.RTPCodecTypeVideo); err != nil {
			log.Printf("[SFU] Warn: registerCodec H264 %s: %v", profile, err)
		}
	}

	// Opus — для аудио (Python aiortc SystemAudioTrack)
	if err := m.RegisterCodec(webrtc.RTPCodecParameters{
		RTPCodecCapability: webrtc.RTPCodecCapability{
			MimeType:    webrtc.MimeTypeOpus,
			ClockRate:   48000,
			Channels:    2,
			SDPFmtpLine: "minptime=10;useinbandfec=1",
		},
		PayloadType: 111,
	}, webrtc.RTPCodecTypeAudio); err != nil {
		log.Printf("[SFU] Warn: registerCodec Opus: %v", err)
	}

	// Интерцепторы: NACK, RTCP reports, PLI
	ir := &interceptor.Registry{}

	intervalPLIFactory, err := intervalpli.NewReceiverInterceptor()
	if err != nil {
		log.Printf("[SFU] Warn: IntervalPLI: %v", err)
	} else {
		ir.Add(intervalPLIFactory)
	}

	if err := webrtc.RegisterDefaultInterceptors(m, ir); err != nil {
		log.Printf("[SFU] Warn: RegisterDefaultInterceptors: %v", err)
	}

	return webrtc.NewAPI(
		webrtc.WithMediaEngine(m),
		webrtc.WithInterceptorRegistry(ir),
	)
}

// peerConfig: без STUN/TURN (стример и SFU на одной машине — localhost ICE).
func localPeerConfig() webrtc.Configuration {
	return webrtc.Configuration{
		ICEServers: []webrtc.ICEServer{},
	}
}

// allRelayTracks возвращает объединённый срез видео + аудио relay-треков.
// Вызывается под mu.Lock или mu.RLock.
func (s *SFU) allRelayTracks() []*webrtc.TrackLocalStaticRTP {
	out := make([]*webrtc.TrackLocalStaticRTP, 0, len(s.videoTracks)+len(s.audioTracks))
	out = append(out, s.videoTracks...)
	out = append(out, s.audioTracks...)
	return out
}

// ─── Streamer (Video, Rust webrtc-rs) ────────────────────────────────────────

// SetStreamerOffer принимает SDP offer от Rust webrtc-rs, создаёт PeerConnection,
// запускает relay и возвращает complete answer (ICE уже собран).
func (s *SFU) SetStreamerOffer(sdpStr string) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	// Если уже есть стример — закрываем старый (только видео PC)
	if s.streamerPC != nil {
		_ = s.streamerPC.Close()
		s.streamerPC = nil
		s.videoTracks = nil
		// audioTracks НЕ сбрасываем — Python audio PC независим
		s.trackReadyCh = make(chan struct{})
		s.trackOnce = sync.Once{}
	}

	pc, err := s.api.NewPeerConnection(localPeerConfig())
	if err != nil {
		return "", fmt.Errorf("NewPeerConnection(streamer): %w", err)
	}
	s.streamerPC = pc

	// OnTrack: Rust отправляет нам видео (и возможно аудио если реализовано)
	pc.OnTrack(func(remoteTrack *webrtc.TrackRemote, receiver *webrtc.RTPReceiver) {
		if remoteTrack.Kind() == webrtc.RTPCodecTypeVideo {
			s.mu.Lock()
			s.streamerSSRC = uint32(remoteTrack.SSRC())
			s.mu.Unlock()
		}
		log.Printf("[SFU] Streamer video track: %s (PT=%d, SSRC=%d)",
			remoteTrack.Codec().MimeType,
			remoteTrack.PayloadType(),
			remoteTrack.SSRC(),
		)

		localTrack, err := webrtc.NewTrackLocalStaticRTP(
			remoteTrack.Codec().RTPCodecCapability,
			remoteTrack.ID(),
			remoteTrack.StreamID(),
		)
		if err != nil {
			log.Printf("[SFU] NewTrackLocalStaticRTP(video) error: %v", err)
			return
		}

		s.mu.Lock()
		s.videoTracks = append(s.videoTracks, localTrack)
		// Добавляем видеотрек ко всем уже подключённым зрителям
		for _, v := range s.viewers {
			if _, addErr := v.pc.AddTrack(localTrack); addErr != nil {
				log.Printf("[SFU] AddVideoTrack to viewer %s: %v", v.id, addErr)
			}
		}
		s.mu.Unlock()

		s.trackOnce.Do(func() { close(s.trackReadyCh) })

		go s.relayRTP(remoteTrack, localTrack)
	})

	pc.OnICEConnectionStateChange(func(state webrtc.ICEConnectionState) {
		log.Printf("[SFU] Streamer ICE: %s", state)
	})

	offer := webrtc.SessionDescription{
		Type: webrtc.SDPTypeOffer,
		SDP:  sdpStr,
	}
	if err := pc.SetRemoteDescription(offer); err != nil {
		return "", fmt.Errorf("SetRemoteDescription(streamer offer): %w", err)
	}

	answer, err := pc.CreateAnswer(nil)
	if err != nil {
		return "", fmt.Errorf("CreateAnswer(streamer): %w", err)
	}

	gatherComplete := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(answer); err != nil {
		return "", fmt.Errorf("SetLocalDescription(streamer): %w", err)
	}
	<-gatherComplete

	localDesc := pc.LocalDescription()
	log.Printf("[SFU] Streamer answer готов (ICE собран), длина SDP: %d", len(localDesc.SDP))

	return localDesc.SDP, nil
}

// CloseStreamer закрывает видео-соединение со стримером.
// audioStreamerPC НЕ закрывается — управляется отдельно.
func (s *SFU) CloseStreamer() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.streamerPC != nil {
		_ = s.streamerPC.Close()
		s.streamerPC = nil
		s.videoTracks = nil
		s.trackReadyCh = make(chan struct{})
		s.trackOnce = sync.Once{}
		log.Println("[SFU] Video streamer connection closed")
	}
}

// ─── Audio Streamer (Python aiortc + InPulseAudioExclusion.dll) ──────────────

// SetAudioStreamerOffer принимает SDP offer от Python aiortc (SystemAudioTrack),
// создаёт отдельный PeerConnection для аудио-трека и добавляет его к зрителям.
//
// FIX: Rust media-engine не имеет доступа к InPulseAudioExclusion.dll.
// Python захватывает системный звук через C++ DLL (WASAPI Process Loopback),
// исключая голоса InPulse по PID. Этот метод принимает audio-only offer
// и relay-ит аудиотрек ко всем зрителям рядом с видеотреком.
func (s *SFU) SetAudioStreamerOffer(sdpStr string) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	// Закрываем старый audio PC если был
	if s.audioStreamerPC != nil {
		_ = s.audioStreamerPC.Close()
		s.audioStreamerPC = nil
		s.audioTracks = nil
		log.Println("[SFU] Старый audio streamer PC закрыт")
	}

	pc, err := s.api.NewPeerConnection(localPeerConfig())
	if err != nil {
		return "", fmt.Errorf("NewPeerConnection(audio): %w", err)
	}
	s.audioStreamerPC = pc

	pc.OnTrack(func(remoteTrack *webrtc.TrackRemote, receiver *webrtc.RTPReceiver) {
		log.Printf("[SFU] Audio streamer track: %s (PT=%d, SSRC=%d)",
			remoteTrack.Codec().MimeType,
			remoteTrack.PayloadType(),
			remoteTrack.SSRC(),
		)

		localTrack, err := webrtc.NewTrackLocalStaticRTP(
			remoteTrack.Codec().RTPCodecCapability,
			remoteTrack.ID(),
			remoteTrack.StreamID(),
		)
		if err != nil {
			log.Printf("[SFU] NewTrackLocalStaticRTP(audio) error: %v", err)
			return
		}

		s.mu.Lock()
		s.audioTracks = append(s.audioTracks, localTrack)
		// Добавляем аудиотрек ко всем уже подключённым зрителям
		for _, v := range s.viewers {
			if _, addErr := v.pc.AddTrack(localTrack); addErr != nil {
				log.Printf("[SFU] AddAudioTrack to viewer %s: %v", v.id, addErr)
			}
		}
		// Сигналим о готовности трека (на случай если видео ещё не стартовало)
		s.trackOnce.Do(func() { close(s.trackReadyCh) })
		s.mu.Unlock()

		go s.relayRTP(remoteTrack, localTrack)
	})

	pc.OnICEConnectionStateChange(func(state webrtc.ICEConnectionState) {
		log.Printf("[SFU] Audio streamer ICE: %s", state)
		if state == webrtc.ICEConnectionStateFailed ||
			state == webrtc.ICEConnectionStateDisconnected {
			log.Println("[SFU] Audio streamer disconnected")
		}
	})

	offer := webrtc.SessionDescription{
		Type: webrtc.SDPTypeOffer,
		SDP:  sdpStr,
	}
	if err := pc.SetRemoteDescription(offer); err != nil {
		return "", fmt.Errorf("SetRemoteDescription(audio offer): %w", err)
	}

	answer, err := pc.CreateAnswer(nil)
	if err != nil {
		return "", fmt.Errorf("CreateAnswer(audio): %w", err)
	}

	gatherComplete := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(answer); err != nil {
		return "", fmt.Errorf("SetLocalDescription(audio): %w", err)
	}
	<-gatherComplete

	localDesc := pc.LocalDescription()
	log.Printf("[SFU] Audio streamer answer готов (ICE собран), длина SDP: %d", len(localDesc.SDP))

	return localDesc.SDP, nil
}

// CloseAudioStreamer закрывает аудио-соединение (Python DLL capture).
func (s *SFU) CloseAudioStreamer() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.audioStreamerPC != nil {
		_ = s.audioStreamerPC.Close()
		s.audioStreamerPC = nil
		s.audioTracks = nil
		log.Println("[SFU] Audio streamer connection closed")
	}
}

// relayRTP читает RTP пакеты от стримера и пишет в localTrack (fan-out).
// Используется как для видео, так и для аудио треков.
func (s *SFU) relayRTP(remote *webrtc.TrackRemote, local *webrtc.TrackLocalStaticRTP) {
	log.Printf("[SFU] Relay goroutine запущена: %s", remote.Codec().MimeType)
	buf := make([]byte, 1600)
	for {
		n, _, err := remote.Read(buf)
		if err != nil {
			if err != io.EOF {
				log.Printf("[SFU] relayRTP read error: %v", err)
			}
			return
		}
		if _, err := local.Write(buf[:n]); err != nil && err != io.ErrClosedPipe {
			log.Printf("[SFU] relayRTP write error: %v", err)
		}
	}
}

// ─── Viewer ───────────────────────────────────────────────────────────────────

// AddViewer принимает SDP offer от aiortc, создаёт viewer PC,
// добавляет relay-треки (видео + аудио) и возвращает complete answer.
func (s *SFU) AddViewer(viewerID, sdpStr string) (string, error) {
	if viewerID == "" {
		viewerID = uuid.NewString()
	}

	// Ждём готовности relay-трека (макс. 30 сек)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	select {
	case <-s.trackReadyCh:
		// Трек готов
	case <-ctx.Done():
		log.Printf("[SFU] Viewer %s: стример не готов, подключаем без треков", viewerID)
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	// Закрываем старый viewer с тем же ID
	if old, ok := s.viewers[viewerID]; ok {
		_ = old.pc.Close()
		close(old.done)
		delete(s.viewers, viewerID)
	}

	pc, err := s.api.NewPeerConnection(localPeerConfig())
	if err != nil {
		return "", fmt.Errorf("NewPeerConnection(viewer %s): %w", viewerID, err)
	}

	doneCh := make(chan struct{})
	conn := &viewerConn{id: viewerID, pc: pc, done: doneCh}
	s.viewers[viewerID] = conn

	// FIX: добавляем ВСЕ relay-треки: видео (Rust) + аудио (Python DLL)
	for _, localTrack := range s.allRelayTracks() {
		if _, err := pc.AddTrack(localTrack); err != nil {
			log.Printf("[SFU] AddTrack to new viewer %s: %v", viewerID, err)
		}
	}

	pc.OnICEConnectionStateChange(func(state webrtc.ICEConnectionState) {
		log.Printf("[SFU] Viewer %s ICE: %s", viewerID, state)
		switch state {
		case webrtc.ICEConnectionStateConnected:
			// PLI burst: шлём 3 PLI с задержкой 300 мс
			go func() {
				for attempt := 1; attempt <= 3; attempt++ {
					if attempt > 1 {
						time.Sleep(300 * time.Millisecond)
					}
					s.mu.RLock()
					streamerPC := s.streamerPC
					ssrc := s.streamerSSRC
					s.mu.RUnlock()

					if streamerPC == nil || ssrc == 0 {
						return
					}
					pli := []rtcp.Packet{&rtcp.PictureLossIndication{MediaSSRC: ssrc}}
					if err := streamerPC.WriteRTCP(pli); err != nil {
						log.Printf("[SFU] PLI #%d ошибка (viewer %s): %v", attempt, viewerID, err)
						return
					}
					log.Printf("[SFU] PLI #%d отправлен стримеру (viewer %s подключился)", attempt, viewerID)
				}
			}()

		case webrtc.ICEConnectionStateDisconnected,
			webrtc.ICEConnectionStateFailed,
			webrtc.ICEConnectionStateClosed:
			s.removeViewer(viewerID)
		}
	})

	offer := webrtc.SessionDescription{
		Type: webrtc.SDPTypeOffer,
		SDP:  sdpStr,
	}
	if err := pc.SetRemoteDescription(offer); err != nil {
		_ = pc.Close()
		delete(s.viewers, viewerID)
		return "", fmt.Errorf("SetRemoteDescription(viewer %s): %w", viewerID, err)
	}

	answer, err := pc.CreateAnswer(nil)
	if err != nil {
		_ = pc.Close()
		delete(s.viewers, viewerID)
		return "", fmt.Errorf("CreateAnswer(viewer %s): %w", viewerID, err)
	}

	gatherComplete := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(answer); err != nil {
		_ = pc.Close()
		delete(s.viewers, viewerID)
		return "", fmt.Errorf("SetLocalDescription(viewer %s): %w", viewerID, err)
	}
	<-gatherComplete

	localDesc := pc.LocalDescription()
	cleanSDP := stripMDNSCandidates(localDesc.SDP)
	log.Printf("[SFU] Viewer %s answer готов, видео=%d аудио=%d", viewerID, len(s.videoTracks), len(s.audioTracks))

	return cleanSDP, nil
}

// RemoveViewer закрывает viewer PC.
func (s *SFU) RemoveViewer(viewerID string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.removeViewer(viewerID)
}

func (s *SFU) removeViewer(viewerID string) {
	if v, ok := s.viewers[viewerID]; ok {
		_ = v.pc.Close()
		select {
		case <-v.done:
		default:
			close(v.done)
		}
		delete(s.viewers, viewerID)
		log.Printf("[SFU] Viewer %s удалён", viewerID)
	}
}

// ─── SDP utilities ────────────────────────────────────────────────────────────

func stripMDNSCandidates(sdp string) string {
	sep := "\r\n"
	if !strings.Contains(sdp, "\r\n") {
		sep = "\n"
	}
	lines := strings.Split(sdp, sep)
	out := make([]string, 0, len(lines))
	removed := 0
	for _, line := range lines {
		if strings.HasPrefix(line, "a=candidate:") && strings.Contains(line, ".local") {
			removed++
			continue
		}
		out = append(out, line)
	}
	if removed > 0 {
		log.Printf("[SFU] stripMDNSCandidates: убрано %d mDNS кандидат(ов)", removed)
	}
	return strings.Join(out, sep)
}

// ─── Status ───────────────────────────────────────────────────────────────────

type Status struct {
	Streamer       string   `json:"streamer"`
	AudioStreamer  string   `json:"audio_streamer"` // FIX: новое поле
	VideoTracks    int      `json:"video_tracks"`   // FIX: переименовано
	AudioTracks    int      `json:"audio_tracks"`   // FIX: новое поле
	Viewers        int      `json:"viewers"`
	ViewerIDs      []string `json:"viewer_ids"`
}

func (s *SFU) Status() Status {
	s.mu.RLock()
	defer s.mu.RUnlock()

	streamerStatus := "none"
	if s.streamerPC != nil {
		streamerStatus = "connected"
	}

	audioStatus := "none"
	if s.audioStreamerPC != nil {
		audioStatus = "connected"
	}

	ids := make([]string, 0, len(s.viewers))
	for id := range s.viewers {
		ids = append(ids, id)
	}

	return Status{
		Streamer:      streamerStatus,
		AudioStreamer: audioStatus,
		VideoTracks:   len(s.videoTracks),
		AudioTracks:   len(s.audioTracks),
		Viewers:       len(s.viewers),
		ViewerIDs:     ids,
	}
}
