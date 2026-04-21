// sfu.go — Ядро Pion SFU: relay треков от стримера к зрителям
//
// v1.1 — Исправления критических и высокоприоритетных багов
//
// ── Что изменилось относительно v1.0 ────────────────────────────────────────
//
//   FIX #26 (КРИТИЧНО): ICE Disconnected больше НЕ триггерит RemoveViewer.
//     Disconnected — ВРЕМЕННОЕ состояние, на RadminVPN типичны 2-5 сек лаги
//     с автовосстановлением. Старый код убивал рабочий PC при каждом глитче.
//     Теперь RemoveViewer только на Failed и Closed.
//
//   FIX #27 (КРИТИЧНО): OnICEConnectionStateChange регистрируется ПОСЛЕ
//     отпускания s.mu. Раньше callback мог вызвать s.RemoveViewer внутри
//     удерживаемого Lock → deadlock. Pion обычно вызывает из отдельной
//     горутины, но формально это была гонка.
//
//   FIX #28: убран противоречивый комментарий "1 секунда vs 3 секунды" про PLI.
//     Значение 3 секунды — актуальное, взято из v1.0 FIX 7. Старый комментарий
//     FIX 5 противоречил коду.
//
//   FIX #33: RTCP-горутины закрываются через ctx.Done() при RemoveViewer.
//     Раньше readViewerRTCP продолжал писать в lossStats после delete,
//     создавая устаревшие записи.
//
//   FIX #38: SetAudioStreamerOffer НЕ трогает trackReadyCh.
//     Раньше обработчик аудио-трека тоже закрывал trackReadyCh через Once,
//     в результате зритель, ожидающий ВИДЕО, мог разбудиться на аудио-треке
//     и уйти в AddViewer до того как появился видео-трек.
//     Теперь trackReadyCh сигнализирует только о видео.
//
//   FIX #39: dropCount теперь реально инкрементируется в broadcast().
//     Раньше поле было, но не использовалось.
//
//   FIX #40: sync.Pool для RTP-пакетов — уменьшает GC pressure на hot path.

package main

import (
	"context"
	"fmt"
	"io"
	"log"
	"math"
	"strings"
	"sync"
	"sync/atomic"
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
const viewerPktBuf = 2048

// FIX: sync.Pool для RTP-пакетов на hot path (relayBroadcast → viewerWriter).
//
// RTP-пакеты в сети ≤ MTU = 1500 байт. Pool хранит срезы ровно по 1500 байт.
// relayBroadcast берёт срез из пула, копирует n ≤ 1500 байт и отправляет
// в channel как pooledPkt. viewerWriter после lt.Write() возвращает срез
// в пул через pooledPkt.free().
//
// Эффект: ~60 аллокаций/сек (30fps × 2 трека) → ~0 аллокаций/сек на hot path.
// Без Pool каждый RTP-пакет создаёт новый объект в GC heap.
var rtpBufPool = sync.Pool{
	New: func() interface{} {
		b := make([]byte, 1500)
		return &b
	},
}

// pooledPkt — RTP-пакет из sync.Pool. После использования вызвать free().
type pooledPkt struct {
	data []byte  // срез пула длиной n (реальный размер пакета)
	ref  *[]byte // указатель на pool-объект для возврата; nil = не из пула
}

func (p pooledPkt) free() {
	if p.ref != nil {
		rtpBufPool.Put(p.ref)
	}
}

// ─── broadcasterTrack ────────────────────────────────────────────────────────

type viewerSub struct {
	track *webrtc.TrackLocalStaticRTP
	ch    chan pooledPkt
}

type broadcasterTrack struct {
	mu       sync.RWMutex
	subs     map[string]*viewerSub
	codec    webrtc.RTPCodecCapability
	id       string
	streamID string

	// FIX #39: dropCount — атомарный счётчик для диагностики.
	dropCount atomic.Uint64
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

func (b *broadcasterTrack) subscribe(
	pc *webrtc.PeerConnection,
	viewerID string,
) (*webrtc.TrackLocalStaticRTP, chan pooledPkt, error) {
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

	ch := make(chan pooledPkt, viewerPktBuf)

	b.mu.Lock()
	b.subs[viewerID] = &viewerSub{track: lt, ch: ch}
	b.mu.Unlock()

	return lt, ch, nil
}

func (b *broadcasterTrack) unsubscribe(viewerID string) {
	b.mu.Lock()
	if sub, ok := b.subs[viewerID]; ok {
		close(sub.ch)
		// Drain оставшихся пакетов — возвращаем в пул.
		for pkt := range sub.ch {
			pkt.free()
		}
		delete(b.subs, viewerID)
	}
	b.mu.Unlock()
}

func (b *broadcasterTrack) unsubscribeAll() {
	b.mu.Lock()
	for id, sub := range b.subs {
		close(sub.ch)
		for pkt := range sub.ch {
			pkt.free()
		}
		delete(b.subs, id)
	}
	b.mu.Unlock()
}

// broadcast: drop-oldest стратегия.
// FIX #39: считаем дропы через atomic.
// FIX pool: каждому зрителю отправляем КОПИЮ пакета из пула.
// Нельзя отправить один pooledPkt нескольким — каждый viewerWriter должен
// самостоятельно вернуть свой срез в пул после Write().
func (b *broadcasterTrack) broadcast(src []byte) {
	b.mu.RLock()
	defer b.mu.RUnlock()
	for _, sub := range b.subs {
		// Берём срез из пула для каждого зрителя отдельно.
		ref := rtpBufPool.Get().(*[]byte)
		raw := (*ref)[:len(src)]
		copy(raw, src)
		pkt := pooledPkt{data: raw, ref: ref}

		select {
		case sub.ch <- pkt:
		default:
			// Канал переполнен: дропаем самый старый пакет, ставим новый.
			select {
			case old := <-sub.ch:
				old.free()
				b.dropCount.Add(1)
			default:
			}
			select {
			case sub.ch <- pkt:
			default:
				pkt.free()
				b.dropCount.Add(1)
			}
		}
	}
}

// DropCount возвращает текущее число дропов (для /status или мониторинга).
func (b *broadcasterTrack) DropCount() uint64 {
	return b.dropCount.Load()
}

// viewerWriter — горутина на (viewer × track).
// После lt.Write() возвращает пакет в pool — нулевые аллокации на hot path.
func viewerWriter(viewerID string, lt *webrtc.TrackLocalStaticRTP, ch <-chan pooledPkt) {
	written := 0
	errors := 0
	for pkt := range ch {
		_, err := lt.Write(pkt.data)
		pkt.free() // возвращаем в пул сразу после Write, независимо от ошибки
		if err != nil {
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
			log.Printf("[SFU] viewerWriter %s: first pkt written OK (len=%d)", viewerID, len(pkt.data))
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

	// FIX #38: trackReadyCh сигнализирует только о готовности ВИДЕО-трека.
	// Аудио-трек не закрывает этот канал — зритель ждёт именно видео.
	trackReadyCh chan struct{}
	trackOnce    sync.Once

	// SENIOR FIX: канал сигнализации о том, что стример отвалился.
	// AddViewer ждёт trackReadyCh до 30 сек — если стример за это время
	// отключился (CloseStreamer), мы закрываем streamerGoneCh и все
	// ожидающие зрители немедленно уходят с ошибкой вместо таймаута.
	streamerGoneCh   chan struct{}
	streamerGoneOnce sync.Once

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
	id     string
	pc     *webrtc.PeerConnection
	done   chan struct{}
	ctx    context.Context
	cancel context.CancelFunc
}

func NewSFU() *SFU {
	return &SFU{
		api:            buildWebRTCAPI(),
		viewers:        make(map[string]*viewerConn),
		trackReadyCh:   make(chan struct{}),
		streamerGoneCh: make(chan struct{}),
		lossStats:      make(map[string]*viewerLossInfo),
	}
}

// Close корректно останавливает SFU: закрывает всех зрителей и стримеров.
func (s *SFU) Close() {
	s.mu.Lock()
	defer s.mu.Unlock()

	if s.streamerPC != nil {
		_ = s.streamerPC.Close()
		s.streamerPC = nil
	}
	if s.audioStreamerPC != nil {
		_ = s.audioStreamerPC.Close()
		s.audioStreamerPC = nil
	}

	for _, bc := range s.videoBcs {
		bc.unsubscribeAll()
	}
	s.videoBcs = nil
	for _, bc := range s.audioBcs {
		bc.unsubscribeAll()
	}
	s.audioBcs = nil

	for id, v := range s.viewers {
		_ = v.pc.Close()
		v.cancel()
		delete(s.viewers, id)
	}

	// SENIOR FIX: сигналим всем AddViewer'ам ждущим trackReadyCh —
	// при graceful shutdown они не должны висеть до таймаута.
	s.streamerGoneOnce.Do(func() { close(s.streamerGoneCh) })
}

func buildWebRTCAPI() *webrtc.API {
	m := &webrtc.MediaEngine{}

	h264Feedback := []webrtc.RTCPFeedback{
		{Type: "goog-remb"},
		{Type: "ccm", Parameter: "fir"},
		{Type: "nack"},
		{Type: "nack", Parameter: "pli"},
	}

	type h264Profile struct {
		pt   webrtc.PayloadType
		fmtp string
	}
	h264Profiles := []h264Profile{
		{pt: 96, fmtp: "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=640032"},
		{pt: 97, fmtp: "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f"},
		{pt: 98, fmtp: "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42001f"},
		{pt: 99, fmtp: "level-asymmetry-allowed=1;packetization-mode=0;profile-level-id=42001f"},
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

	// FIX #28: PLI interval — 3 секунды. Слишком частые PLI приводили к
	// постоянным IDR-burst'ам, которые тяжелы для RadminVPN.
	if f, err := intervalpli.NewReceiverInterceptor(
		intervalpli.GeneratorInterval(3 * time.Second),
	); err != nil {
		log.Printf("[SFU] PLI interceptor error: %v", err)
	} else {
		ir.Add(f)
		log.Printf("[SFU] PLI interval: 3s")
	}

	if responder, err := nack.NewResponderInterceptor(
		nack.ResponderSize(2048),
	); err != nil {
		log.Printf("[SFU] NACK responder init error: %v", err)
	} else {
		ir.Add(responder)
		log.Printf("[SFU] NACK responder: buf=2048 pkt")
	}

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

	// SENIOR FIX: если был активен streamerGoneCh (предыдущий стример ушёл) —
	// пересоздаём его, чтобы новый AddViewer не получил моментальный fail
	// из-за закрытого канала от прошлой сессии.
	select {
	case <-s.streamerGoneCh:
		// уже закрыт — создаём новый
		s.streamerGoneCh = make(chan struct{})
		s.streamerGoneOnce = sync.Once{}
	default:
		// канал ещё открыт — ничего не делаем
	}

	pc, err := s.api.NewPeerConnection(localPeerConfig())
	if err != nil {
		s.mu.Unlock()
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
		for _, v := range s.viewers {
			lt, ch, err := bc.subscribe(v.pc, v.id)
			if err != nil {
				log.Printf("[SFU] subscribe viewer %s: %v", v.id, err)
				continue
			}
			go viewerWriter(v.id, lt, ch)
		}
		// FIX #38: trackReadyCh закрываем ТОЛЬКО для видео.
		isVideo := rt.Kind() == webrtc.RTPCodecTypeVideo
		s.mu.Unlock()

		if isVideo {
			s.trackOnce.Do(func() { close(s.trackReadyCh) })
		}
		go s.relayBroadcast(rt, bc)
	})

	pc.OnICEConnectionStateChange(func(st webrtc.ICEConnectionState) {
		log.Printf("[SFU] Streamer ICE: %s", st)
	})

	if err := pc.SetRemoteDescription(webrtc.SessionDescription{
		Type: webrtc.SDPTypeOffer, SDP: sdpStr,
	}); err != nil {
		s.mu.Unlock()
		return "", fmt.Errorf("SetRemoteDescription: %w", err)
	}
	ans, err := pc.CreateAnswer(nil)
	if err != nil {
		s.mu.Unlock()
		return "", fmt.Errorf("CreateAnswer: %w", err)
	}
	gc := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(ans); err != nil {
		s.mu.Unlock()
		return "", err
	}

	// FIX: unlock ПЕРЕД блокирующим <-gc (ICE gathering = UDP операции).
	// Раньше стоял defer s.mu.Unlock(), и вся горутина ICE gathering
	// (включая отправку/приём STUN) выполнялась под глобальным мьютексом SFU.
	// Любой одновременный AddViewer или RemoveViewer вставал в очередь на
	// всё время gather (десятки мс на LAN, секунды при проблемах с сетью).
	s.mu.Unlock()

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

	// SENIOR FIX: сигналим всем AddViewer'ам ждущим trackReadyCh чтобы они
	// не сидели до таймаута 30 сек. Закрываем текущий streamerGoneCh
	// (все ожидающие получают сигнал) и пересоздаём свежий под новые
	// сессии стримера.
	s.streamerGoneOnce.Do(func() { close(s.streamerGoneCh) })
	s.streamerGoneCh = make(chan struct{})
	s.streamerGoneOnce = sync.Once{}

	s.trackReadyCh = make(chan struct{})
	s.trackOnce = sync.Once{}
}

// ─── Audio Streamer ───────────────────────────────────────────────────────────

func (s *SFU) SetAudioStreamerOffer(sdpStr string) (string, error) {
	s.mu.Lock()

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
		s.mu.Unlock()
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
		// FIX #38: НЕ трогаем trackReadyCh здесь — это только для видео.
		s.mu.Unlock()

		go s.relayBroadcast(rt, bc)
	})

	pc.OnICEConnectionStateChange(func(st webrtc.ICEConnectionState) {
		log.Printf("[SFU] Audio ICE: %s", st)
	})

	if err := pc.SetRemoteDescription(webrtc.SessionDescription{
		Type: webrtc.SDPTypeOffer, SDP: sdpStr,
	}); err != nil {
		s.mu.Unlock()
		return "", err
	}
	ans, err := pc.CreateAnswer(nil)
	if err != nil {
		s.mu.Unlock()
		return "", err
	}
	gc := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(ans); err != nil {
		s.mu.Unlock()
		return "", err
	}

	// FIX: аналогично SetStreamerOffer — unlock перед ICE gathering.
	s.mu.Unlock()

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
// read buffer (16 KB) переиспользуется между итерациями — одна аллокация
// на горутину за всё время жизни. Копии для зрителей берутся из sync.Pool
// внутри broadcast() — нулевые heap-аллокации на hot path при наличии зрителей.
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
			log.Printf("[SFU] relayBroadcast ended for %s/%s (total pkts: %d, drops: %d)",
				bc.codec.MimeType, bc.id, pktCount, bc.DropCount())
			return
		}
		pktCount++

		select {
		case <-logInterval.C:
			bc.mu.RLock()
			nSubs := len(bc.subs)
			bc.mu.RUnlock()
			log.Printf("[SFU] relay %s: %d pkts forwarded, %d viewers, %d drops",
				bc.codec.MimeType, pktCount, nSubs, bc.DropCount())
		default:
		}

		// broadcast() копирует buf[:n] в pool-буфер для каждого зрителя.
		// Сам buf переиспользуется на следующей итерации — безопасно,
		// потому что копирование происходит внутри broadcast() до возврата.
		bc.broadcast(buf[:n])
	}
}

// ─── Viewer ───────────────────────────────────────────────────────────────────

func (s *SFU) AddViewer(viewerID, sdpStr string) (string, error) {
	if viewerID == "" {
		viewerID = uuid.NewString()
	}

	// Snapshot trackReadyCh и streamerGoneCh под RLock
	s.mu.RLock()
	readyCh := s.trackReadyCh
	goneCh := s.streamerGoneCh
	s.mu.RUnlock()

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	// SENIOR FIX: добавлен select-case на streamerGoneCh.
	// Если стример отвалился пока мы ждали — выходим немедленно
	// вместо 30 сек таймаута.
	select {
	case <-readyCh:
		// видео-трек готов — продолжаем подписку
	case <-goneCh:
		log.Printf("[SFU] AddViewer %s: streamer gone while waiting", viewerID)
		return "", fmt.Errorf("streamer disconnected")
	case <-ctx.Done():
		log.Printf("[SFU] AddViewer %s: timeout waiting for streamer track", viewerID)
	}

	s.mu.Lock()

	if old, ok := s.viewers[viewerID]; ok {
		_ = old.pc.Close()
		old.cancel() // FIX #33: отменяем старые RTCP-горутины
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
		s.mu.Unlock()
		return "", err
	}
	vCtx, vCancel := context.WithCancel(context.Background())
	conn := &viewerConn{
		id: viewerID, pc: pc,
		done:   make(chan struct{}),
		ctx:    vCtx,
		cancel: vCancel,
	}
	s.viewers[viewerID] = conn

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

	// Собираем SDP под lock, чтобы никто не удалил viewer пока мы ещё не
	// завершили offer/answer handshake.
	if err := pc.SetRemoteDescription(webrtc.SessionDescription{
		Type: webrtc.SDPTypeOffer, SDP: sdpStr,
	}); err != nil {
		delete(s.viewers, viewerID)
		s.mu.Unlock()
		pc.Close()
		vCancel()
		return "", err
	}
	ans, err := pc.CreateAnswer(nil)
	if err != nil {
		delete(s.viewers, viewerID)
		s.mu.Unlock()
		pc.Close()
		vCancel()
		return "", err
	}
	gc := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(ans); err != nil {
		delete(s.viewers, viewerID)
		s.mu.Unlock()
		pc.Close()
		vCancel()
		return "", err
	}

	// FIX #27: регистрируем callback ПОСЛЕ отпускания s.mu.
	// Раньше callback мог быть вызван из горутины Pion и попытаться взять s.mu
	// для RemoveViewer — пока мы его держим, это deadlock (хоть и
	// редкий при Pion async-модели, но формально race).
	s.mu.Unlock()

	// Теперь безопасно устанавливаем callback: даже если он вызовется
	// синхронно из SetRemoteDescription/etc — lock уже отпущен.
	pc.OnICEConnectionStateChange(func(st webrtc.ICEConnectionState) {
		log.Printf("[SFU] Viewer %s ICE: %s", viewerID, st)
		switch st {
		case webrtc.ICEConnectionStateConnected:
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
			go s.readViewerRTCP(viewerID, pc, vCtx)

		// FIX #26: НЕ реагируем на Disconnected — это временное состояние,
		// типично для RadminVPN при сетевом лаге. Только Failed/Closed
		// означают окончательный разрыв.
		case webrtc.ICEConnectionStateFailed,
			webrtc.ICEConnectionStateClosed:
			s.RemoveViewer(viewerID)
		}
	})

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
	v.cancel() // FIX #33: отменяем RTCP-горутины
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
// FIX #33: принимает ctx и завершается при ctx.Done() — не пишет в
// lossStats после RemoveViewer.
func (s *SFU) readViewerRTCP(viewerID string, pc *webrtc.PeerConnection, ctx context.Context) {
	for _, recv := range pc.GetReceivers() {
		go func(r *webrtc.RTPReceiver) {
			for {
				// Проверяем ctx перед блокирующим ReadRTCP
				select {
				case <-ctx.Done():
					return
				default:
				}
				pkts, _, err := r.ReadRTCP()
				if err != nil {
					return
				}
				// Ещё раз проверяем ctx — между ReadRTCP и записью в stats
				select {
				case <-ctx.Done():
					return
				default:
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
	VideoDrops    uint64   `json:"video_drops"`
	AudioDrops    uint64   `json:"audio_drops"`
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
	var videoDrops, audioDrops uint64
	for _, bc := range s.videoBcs {
		videoDrops += bc.DropCount()
	}
	for _, bc := range s.audioBcs {
		audioDrops += bc.DropCount()
	}
	return Status{
		Streamer:      ss,
		AudioStreamer: as,
		VideoBcs:      len(s.videoBcs),
		AudioBcs:      len(s.audioBcs),
		Viewers:       len(s.viewers),
		ViewerIDs:     ids,
		VideoDrops:    videoDrops,
		AudioDrops:    audioDrops,
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
