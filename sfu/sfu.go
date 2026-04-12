// sfu.go — Ядро Pion SFU: relay треков от стримера к зрителям
//
// v5 — Исправления критических и высокоприоритетных багов
//
// ── Что изменилось относительно v4 ──────────────────────────────────────────
//
//   FIX 1 (КРИТИЧНО): H.264 профили теперь имеют УНИКАЛЬНЫЕ Payload Type.
//     Было: все 4 профиля с PT=96 → дублирующиеся a=rtpmap в SDP → невалидный
//     SDP, Pion перезаписывал предыдущую запись, итоговый кодек непредсказуем.
//     Стало: PT 96 (High 5.0), 97 (CB 3.1), 98 (Baseline mode-1), 99 (mode-0).
//     Для screen sharing приоритетен High (96) — лучший compression для UI/текста.
//
//   FIX 2 (КРИТИЧНО): Race condition в AddViewer на trackReadyCh.
//     Было: AddViewer читал s.trackReadyCh БЕЗ мьютекса. SetStreamerOffer
//     заменяет канал под Lock. При одновременном реконнекте стримера и
//     подключении зрителя — зритель мог ждать на старом закрытом канале
//     (мгновенный return без трека) или на новом (31s timeout), оба случая
//     приводили к "Ожидание видео...".
//     Стало: snapshot trackReadyCh под RLock перед select.
//
//   FIX 3 (ВЫСОКИЙ): NACK Responder buffer 512 → 2048 пакетов.
//     512 пакетов × 1300 байт (RadminVPN MTU) ≈ 665 KB.
//     При 10 Mbps (1080p) это только 530 мс — мало при burst IDR-кадра.
//     2048 пакетов ≈ 2.6 MB ≈ 2.1 сек при 10 Mbps — достаточно для RTT VPN.
//
//   FIX 4 (ВЫСОКИЙ): NACK Generator явный размер буфера 2048.
//     Дефолт генератора — 512 записей. При 10+ Mbps дыры в seq теряются
//     до того как генератор успевает послать NACK.
//
//   FIX 5 (ВЫСОКИЙ): PLI interval — явный 1 секунда вместо дефолтных 3.
//     При потере пакетов на RadminVPN и NACK failure зритель ждал до 3 сек
//     артефактов/фриза до следующего PLI. 1 сек — хороший компромисс.
//
//   FIX 6 (ВЫСОКИЙ): broadcast() — drop-oldest вместо drop-newest.
//     Было: канал переполнен → новый пакет дропался, зритель получал
//     устаревшие данные из буфера.
//     Стало: при переполнении сначала выбрасываем старый пакет (drain 1),
//     затем кладём новый. Зритель всегда получает актуальный поток.
//     Для видео свежий I-frame важнее чем старые P-frame.
//
//   FIX 7: mDNS отключён на ICE уровне (aiortc зависает на *.local).
//     IP фильтр убран: вызывал рост пинга, принудительно пуская видео
//     через RadminVPN relay вместо прямого LAN-пути.
//     PLI interval: 3s (было 1s — слишком частые IDR-burst'ы).
//
//   БУФЕР: viewerPktBuf = 2048 пакетов ≈ 2.4 МБ при 6 Mbps (1200 байт/пкт).
//   С drop-oldest стратегией большой буфер безопасен — свежие пакеты
//   всегда вытесняют устаревшие.

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
	"github.com/pion/ice/v2"
	"github.com/pion/interceptor"
	"github.com/pion/interceptor/pkg/intervalpli"
	"github.com/pion/interceptor/pkg/nack"
	"github.com/pion/rtcp"
	"github.com/pion/webrtc/v3"
)

// viewerPktBuf — размер канала пакетов на одного зрителя.
// FIX 3: 2048 пакетов (было 1024).
// 2048 × 1200 байт = ~2.4 MB ≈ 3.2 сек при 6 Mbps.
// С drop-oldest стратегией (FIX 6) большой буфер безопасен:
// при переполнении удаляется старый пакет, новый всегда входит.
const viewerPktBuf = 2048

// ─── broadcasterTrack ────────────────────────────────────────────────────────
//
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

	// FIX 6: счётчик дропов для диагностики (per-broadcaster)
	dropCount uint64
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

// subscribe создаёт персональный TrackLocalStaticRTP для зрителя.
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

// unsubscribeAll закрывает каналы всех зрителей (при CloseStreamer).
func (b *broadcasterTrack) unsubscribeAll() {
	b.mu.Lock()
	for id, sub := range b.subs {
		close(sub.ch)
		delete(b.subs, id)
	}
	b.mu.Unlock()
}

// broadcast рассылает пакет всем зрителям.
//
// FIX 6: drop-oldest стратегия вместо drop-newest.
// Если канал зрителя переполнен — удаляем 1 старый пакет, кладём новый.
// Для видео актуальный I-frame важнее устаревших P-frame в буфере.
// Блокировка одного зрителя НЕ влияет на остальных.
func (b *broadcasterTrack) broadcast(pkt []byte) {
	b.mu.RLock()
	defer b.mu.RUnlock()
	for _, sub := range b.subs {
		select {
		case sub.ch <- pkt:
			// Пакет доставлен без задержки
		default:
			// Канал переполнен: дропаем самый старый пакет,
			// освобождаем место для нового (актуального).
			select {
			case <-sub.ch:
				// Дроп одного старого пакета
			default:
			}
			// Повторная попытка вставки нового пакета
			select {
			case sub.ch <- pkt:
			default:
				// Крайне редкий кейс: другой горутин успел занять слот.
				// Пакет теряется — это нормально, NACK запросит повтор.
			}
		}
	}
}

// viewerWriter — горутин на (viewer × track).
// Читает из персонального канала и пишет в TrackLocalStaticRTP.
// Завершается автоматически когда канал закрыт.
func viewerWriter(viewerID string, lt *webrtc.TrackLocalStaticRTP, ch <-chan []byte) {
	written := 0
	errors := 0
	for pkt := range ch {
		if _, err := lt.Write(pkt); err != nil {
			if errors == 0 {
				log.Printf("[SFU] viewerWriter %s: first write error: %v", viewerID, err)
			}
			errors++
			if errors > 100 {
				log.Printf("[SFU] viewerWriter %s: too many errors (%d), exiting", viewerID, errors)
				return
			}
			continue
		}
		written++
		if written == 1 {
			log.Printf("[SFU] viewerWriter %s: first pkt written OK (len=%d)", viewerID, len(pkt))
		}
	}
	log.Printf("[SFU] viewerWriter %s: channel closed (written=%d, errors=%d)", viewerID, written, errors)
}

// ─── SFU ─────────────────────────────────────────────────────────────────────

type SFU struct {
	mu  sync.RWMutex
	api *webrtc.API

	streamerPC      *webrtc.PeerConnection
	streamerSSRC    uint32
	audioStreamerPC *webrtc.PeerConnection

	videoBcs []*broadcasterTrack
	audioBcs []*broadcasterTrack

	// FIX 2: trackReadyCh защищён mu. Читать только под RLock (snapshot).
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

	// FIX 1: Уникальные Payload Type для каждого H.264 профиля.
	//
	// Было: все профили с PT=96 → дублирующиеся a=rtpmap:96 в SDP.
	// Pion перезаписывает предыдущую запись при одинаковом PT.
	// Итоговый кодек зависел от порядка RegisterCodec — непредсказуемо.
	//
	// Стало: уникальные PT согласно RFC 3551 (динамический диапазон 96-127).
	//
	// PT 96 — High Profile Level 5.0 (640032):
	//   Приоритетный кодек для screen sharing 1080p+.
	//   NVENC/AMF кодируют в High Profile по умолчанию.
	//   Лучший compression для UI-контента с большими плоскими областями.
	//
	// PT 97 — Constrained Baseline Level 3.1 (42e01f):
	//   Максимальная совместимость (все декодеры включая мобильные).
	//   Fallback если зритель не поддерживает High.
	//
	// PT 98 — Baseline Level 3.1 mode-1 (42001f, packetization-mode=1):
	//   Для декодеров без поддержки Constrained Baseline.
	//
	// PT 99 — Baseline Level 3.1 mode-0 (42001f, packetization-mode=0):
	//   Крайний fallback. Fragmentation Unit A (FU-A) не поддерживается —
	//   большие NALUs должны влезать в один RTP-пакет.
	type h264Profile struct {
		pt   webrtc.PayloadType
		fmtp string
	}
	h264Profiles := []h264Profile{
		{
			pt:   96,
			fmtp: "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=640032",
		},
		{
			pt:   97,
			fmtp: "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
		},
		{
			pt:   98,
			fmtp: "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42001f",
		},
		{
			pt:   99,
			fmtp: "level-asymmetry-allowed=1;packetization-mode=0;profile-level-id=42001f",
		},
	}
	for _, p := range h264Profiles {
		if err := m.RegisterCodec(webrtc.RTPCodecParameters{
			RTPCodecCapability: webrtc.RTPCodecCapability{
				MimeType:     webrtc.MimeTypeH264,
				ClockRate:    90000,
				SDPFmtpLine:  p.fmtp,
				RTCPFeedback: h264Feedback,
			},
			PayloadType: p.pt,
		}, webrtc.RTPCodecTypeVideo); err != nil {
			log.Printf("[SFU] RegisterCodec PT=%d error: %v", p.pt, err)
		}
	}

	if err := m.RegisterCodec(webrtc.RTPCodecParameters{
		RTPCodecCapability: webrtc.RTPCodecCapability{
			MimeType:    webrtc.MimeTypeOpus,
			ClockRate:   48000,
			Channels:    2,
			SDPFmtpLine: "minptime=10;useinbandfec=1;usedtx=1",
		},
		PayloadType: 111,
	}, webrtc.RTPCodecTypeAudio); err != nil {
		log.Printf("[SFU] RegisterCodec Opus error: %v", err)
	}

	ir := &interceptor.Registry{}

	// FIX 5: PLI interval явный — 1 секунда вместо дефолтных 3.
	// При потере пакетов и NACK failure зритель ждал до 3 сек артефактов.
	// На RadminVPN с джиттером это ощутимо. 1 сек — хороший баланс
	// между частотой PLI-трафика и временем восстановления картинки.
	if f, err := intervalpli.NewReceiverInterceptor(
		intervalpli.GeneratorInterval(3 * time.Second),
	); err != nil {
		log.Printf("[SFU] PLI interceptor error: %v", err)
	} else {
		ir.Add(f)
		log.Printf("[SFU] PLI interval: 3s")
	}

	// FIX 3: NACK Responder buffer 512 → 2048 пакетов.
	// 512 × 1300 байт ≈ 665 KB — мало при 10 Mbps (530 мс).
	// IDR-кадр может весить 200-400 KB burst'ом в начале GOP.
	// 2048 × 1300 байт ≈ 2.6 MB ≈ 2.1 сек при 10 Mbps.
	// Это покрывает несколько RTT RadminVPN с запасом.
	if responder, err := nack.NewResponderInterceptor(
		nack.ResponderSize(2048),
	); err != nil {
		log.Printf("[SFU] NACK responder init error: %v", err)
	} else {
		ir.Add(responder)
		log.Printf("[SFU] NACK responder: buf=2048 pkt")
	}

	// FIX 4: NACK Generator с явным размером буфера 2048.
	// Дефолтный генератор имеет фиксированный receiver queue.
	// При высоком битрейте дыры в seq могут выпасть из окна
	// до того как генератор успеет их задетектировать и послать NACK.
	if generator, err := nack.NewGeneratorInterceptor(
		nack.GeneratorSize(2048),
	); err != nil {
		log.Printf("[SFU] NACK generator init error: %v", err)
	} else {
		ir.Add(generator)
		log.Printf("[SFU] NACK generator: buf=2048")
	}

	if err := webrtc.ConfigureRTCPReports(ir); err != nil {
		log.Printf("[SFU] RTCP reports error: %v", err)
	}

	// SettingEngine: отключаем только mDNS.
	//
	// mDNS ОТКЛЮЧЁН — aiortc на Windows зависает при резолве *.local имён.
	//
	// IP ФИЛЬТР УБРАН — он вызывал рост пинга у всех пользователей:
	//   Фильтр разрешал только 26.x.x.x (RadminVPN) и loopback.
	//   Если ноутбук и ПК на одном LAN/WiFi, SFU включал в ICE answer
	//   только RadminVPN-кандидатов, игнорируя прямой LAN (192.168.x.x).
	//   ICE выбирал RadminVPN. При relay через интернет — весь поток видео
	//   5-6 Mbps шёл через интернет-канал → насыщал его → рос пинг.
	//   Без фильтра ICE сам выбирает лучший путь: LAN или RadminVPN.
	settingEngine := webrtc.SettingEngine{}

	settingEngine.SetICEMulticastDNSMode(ice.MulticastDNSModeDisabled)
	log.Printf("[SFU] ICE: mDNS disabled, no IP filter (ICE auto-selects best path)")

	return webrtc.NewAPI(
		webrtc.WithMediaEngine(m),
		webrtc.WithInterceptorRegistry(ir),
		webrtc.WithSettingEngine(settingEngine),
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
	// Отписываем всех зрителей от старых трансляций
	for _, bc := range s.videoBcs {
		bc.unsubscribeAll()
	}
	s.videoBcs = nil

	// FIX 2: trackReadyCh меняется под мьютексом.
	// AddViewer делает snapshot под RLock — они не пересекаются.
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
			go viewerWriter(v.id, lt, ch)
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
	// FIX 2: trackReadyCh сбрасывается под Lock — безопасно.
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
			go viewerWriter(v.id, lt, ch)
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

// ─── relayBroadcast ──────────────────────────────────────────────────────────
//
// Читает RTP пакеты от стримера и рассылает через broadcasterTrack.broadcast().
// Каждый зритель — свой буферизованный канал и свой горутин viewerWriter.
// Медленный зритель не блокирует поток пакетов для остальных.
//
// Буфер 16384: keyframe NALU может прийти одним большим пакетом (8–32 KB
// на loopback без MTU 1500). Меньший буфер → усечение → битый H264 → артефакт.
func (s *SFU) relayBroadcast(remote *webrtc.TrackRemote, bc *broadcasterTrack) {
	buf := make([]byte, 16384)
	pktCount := 0
	logInterval := time.NewTicker(5 * time.Second)
	defer logInterval.Stop()

	for {
		n, _, err := remote.Read(buf)
		if err != nil {
			if err != io.EOF {
				log.Printf("[SFU] relay read: %v", err)
			}
			log.Printf("[SFU] relayBroadcast ended for %s/%s (total pkts: %d)",
				bc.codec.MimeType, bc.id, pktCount)
			return
		}
		pktCount++

		select {
		case <-logInterval.C:
			bc.mu.RLock()
			nSubs := len(bc.subs)
			bc.mu.RUnlock()
			log.Printf("[SFU] relay %s: %d pkts forwarded, %d viewers",
				bc.codec.MimeType, pktCount, nSubs)
		default:
		}

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

	// FIX 2: Snapshot trackReadyCh под RLock перед ожиданием.
	//
	// ПРОБЛЕМА (было): s.trackReadyCh читался без мьютекса.
	// SetStreamerOffer заменяет канал под полным Lock.
	// Race: зритель мог получить ссылку на старый канал (уже закрытый →
	// немедленный return без трека) или на момент замены — непредсказуемо.
	// Симптом: "Ожидание видео..." при реконнекте стримера.
	//
	// РЕШЕНИЕ: берём RLock, копируем ссылку на текущий канал, отпускаем.
	// SetStreamerOffer использует полный Lock — они не пересекаются.
	// Зритель всегда ждёт на актуальной версии канала.
	s.mu.RLock()
	readyCh := s.trackReadyCh
	s.mu.RUnlock()

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	select {
	case <-readyCh:
	case <-ctx.Done():
		log.Printf("[SFU] AddViewer %s: timeout waiting for streamer track", viewerID)
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	// Закрываем старое соединение если viewer переподключается
	if old, ok := s.viewers[viewerID]; ok {
		_ = old.pc.Close()
		select {
		case <-old.done:
		default:
			close(old.done)
		}
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
	for _, bc := range s.videoBcs {
		lt, ch, err := bc.subscribe(pc, viewerID)
		if err != nil {
			log.Printf("[SFU] video subscribe %s: %v", viewerID, err)
			continue
		}
		go viewerWriter(viewerID, lt, ch)
	}
	for _, bc := range s.audioBcs {
		lt, ch, err := bc.subscribe(pc, viewerID)
		if err != nil {
			log.Printf("[SFU] audio subscribe %s: %v", viewerID, err)
			continue
		}
		go viewerWriter(viewerID, lt, ch)
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

// readViewerRTCP читает RTCP от зрителя (Receiver Reports для ABR).
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
	AudioStreamer  string   `json:"audio_streamer"`
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
		Streamer:     ss,
		AudioStreamer: as,
		VideoBcs:     len(s.videoBcs),
		AudioBcs:     len(s.audioBcs),
		Viewers:      len(s.viewers),
		ViewerIDs:    ids,
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
	removed := 0
	for _, l := range lines {
		if strings.HasPrefix(l, "a=candidate:") && strings.Contains(l, ".local") {
			removed++
			continue
		}
		out = append(out, l)
	}
	if removed > 0 {
		log.Printf("[SFU] stripMDNSCandidates: убрано %d mDNS кандидат(ов)", removed)
	}
	return strings.Join(out, sep)
}
