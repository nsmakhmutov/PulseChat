// src/pipeline/mod.rs — Конвейер: WGC Capture → HW Encode → WebRTC → Pion SFU
//
// v3: Named Pipe заменён на webrtc-rs.
//     Вместо write_frame() → pipe → Python → aiortc → SFU
//     теперь:   sender.push_frame() → webrtc-rs → RTP → Pion SFU
//
// Жизненный цикл:
//   1. Pipeline::start(config) — создаёт WebRtcSender, эмитит WEBRTC_OFFER,
//      запускает capture + encode threads.
//   2. pipeline.set_webrtc_answer(sdp) — Python вернул answer от SFU.
//   3. Кадры идут: capture → encode → push_frame → WebRTC → RTP → SFU.
//   4. pipeline.stop() — останавливает всё, закрывает PC.
//
// FIX: PLI обработка
//   Перед каждым encode() проверяем sender.take_pli_request().
//   Если PLI пришёл от SFU — вызываем encoder.request_keyframe().
//   Это заставляет AMD AMF / NVENC / libx264 выдать IDR-кадр.
//   Без этого зритель вечно получает только P-кадры → decode error.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use bytes::Bytes;
use tokio::sync::mpsc;
use tracing::{error, info, warn};

use crate::capture::{CapturedFrame, ScreenCapture};
use crate::encode::{HwEncoder, LqEncoder};
use crate::ipc::{self, Event};
use crate::webrtc_out::WebRtcSender;

// ─── Config ──────────────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct StreamConfig {
    pub monitor: u32,
    pub width: u32,
    pub height: u32,
    pub fps: u32,
    pub bitrate: u32,
    pub simulcast: bool,
    pub stream_audio: bool,
}

impl StreamConfig {
    pub fn lq_resolution(&self) -> (u32, u32) {
        let w = (self.width / 2).max(320);
        let h = (self.height / 2).max(180);
        (w & !1, h & !1)
    }
    pub fn lq_bitrate(&self) -> u32 {
        (self.bitrate / 4).max(500_000)
    }
}

// ─── Stats ───────────────────────────────────────────────────────────────────

pub struct StreamStats {
    pub frames_captured: AtomicU64,
    pub frames_encoded:  AtomicU64,
    pub frames_dropped:  AtomicU64,
    pub last_fps:        AtomicU64,
}

impl Default for StreamStats {
    fn default() -> Self {
        Self {
            frames_captured: AtomicU64::new(0),
            frames_encoded:  AtomicU64::new(0),
            frames_dropped:  AtomicU64::new(0),
            last_fps:        AtomicU64::new(0),
        }
    }
}

// ─── Encoders wrapper ────────────────────────────────────────────────────────

struct Encoders {
    hq: HwEncoder,
    lq: Option<LqEncoder>,
}
// FFmpeg encoder не Send по умолчанию из-за сырых указателей,
// но мы используем его только в одном потоке — безопасно.
unsafe impl Send for Encoders {}

// ─── Pipeline ────────────────────────────────────────────────────────────────

pub struct Pipeline {
    running:        Arc<AtomicBool>,
    config:         StreamConfig,
    stats:          Arc<StreamStats>,
    webrtc_sender:  Arc<WebRtcSender>,           // для set_webrtc_answer + close
    capture_handle: Option<thread::JoinHandle<()>>,
}

impl Pipeline {
    /// Запускает pipeline:
    /// 1. Создаёт WebRtcSender → эмитит WEBRTC_OFFER
    /// 2. Запускает capture thread
    /// 3. Запускает encode + relay tokio task
    pub async fn start(config: StreamConfig) -> Result<Self> {
        let running = Arc::new(AtomicBool::new(true));
        let stats   = Arc::new(StreamStats::default());

        // ── WebRTC sender ─────────────────────────────────────────────────────
        let (sender, offer_sdp) = WebRtcSender::new(config.fps)
            .await
            .context("WebRtcSender::new")?;

        // Отправляем offer Python-хосту — он должен вернуть WEBRTC_ANSWER
        ipc::send_event(&Event::WebrtcOffer { sdp: offer_sdp })
            .await
            .context("send WEBRTC_OFFER")?;

        info!(
            "Pipeline: {}×{} @ {} fps, {} kbps — ждём WEBRTC_ANSWER",
            config.width, config.height, config.fps, config.bitrate / 1000
        );

        // ── Frame channel (capture → encode) ──────────────────────────────────
        let (frame_tx, mut frame_rx) = mpsc::channel::<CapturedFrame>(4);

        // ── Capture thread ────────────────────────────────────────────────────
        let cap_running = running.clone();
        let cap_stats   = stats.clone();
        let cap_config  = config.clone();

        let capture_handle = thread::Builder::new()
            .name("wgc-capture".into())
            .spawn(move || {
                capture_loop(cap_config, cap_running, cap_stats, frame_tx);
            })
            .context("spawn capture thread")?;

        // ── Encode + relay tokio task ─────────────────────────────────────────
        let enc_running = running.clone();
        let enc_stats   = stats.clone();
        let enc_config  = config.clone();
        let enc_sender  = Arc::clone(&sender);

        tokio::spawn(async move {
            if let Err(e) = encode_loop(enc_config, enc_running, enc_stats, &mut frame_rx, enc_sender).await {
                error!("Encode loop error: {e:#}");
                let _ = ipc::send_event(&Event::Error { message: format!("{e}") }).await;
            }
        });

        Ok(Self {
            running,
            config,
            stats,
            webrtc_sender: sender,
            capture_handle: Some(capture_handle),
        })
    }

    /// Вызывается когда Python прислал WEBRTC_ANSWER (SDP от Pion SFU).
    /// После этого ICE начинает коннектиться и кадры пойдут через RTP.
    pub async fn set_webrtc_answer(&self, sdp: String) -> Result<()> {
        self.webrtc_sender.set_answer(sdp).await
    }

    pub fn stats(&self) -> &StreamStats {
        &self.stats
    }

    pub async fn stop(&mut self) -> Result<()> {
        info!("Pipeline: остановка...");
        self.running.store(false, Ordering::Relaxed);

        if let Some(h) = self.capture_handle.take() {
            let _ = h.join();
        }

        self.webrtc_sender.close().await?;

        ipc::send_event(&Event::StreamStopped).await?;
        info!("Pipeline остановлен");
        Ok(())
    }
}

impl Drop for Pipeline {
    fn drop(&mut self) {
        self.running.store(false, Ordering::Relaxed);
    }
}

// ─── Capture loop (dedicated thread) ─────────────────────────────────────────

fn capture_loop(
    config:   StreamConfig,
    running:  Arc<AtomicBool>,
    stats:    Arc<StreamStats>,
    frame_tx: mpsc::Sender<CapturedFrame>,
) {
    info!("Capture thread запущен");

    #[cfg(windows)]
    unsafe {
        use windows::Win32::System::Com::{CoInitializeEx, COINIT_APARTMENTTHREADED};
        let _ = CoInitializeEx(None, COINIT_APARTMENTTHREADED);
    }

    let mut capture = match ScreenCapture::new(config.monitor) {
        Ok(c) => c,
        Err(e) => {
            error!("WGC init error: {e:#}");
            ipc::send_event_sync(&Event::Error { message: format!("WGC: {e}") });
            return;
        }
    };

    let frame_dur = Duration::from_secs_f64(1.0 / config.fps as f64);

    while running.load(Ordering::Relaxed) {
        let t = Instant::now();

        match capture.grab() {
            Ok(Some(frame)) => {
                stats.frames_captured.fetch_add(1, Ordering::Relaxed);
                match frame_tx.try_send(frame) {
                    Ok(()) => {}
                    Err(mpsc::error::TrySendError::Full(_)) => {
                        stats.frames_dropped.fetch_add(1, Ordering::Relaxed);
                    }
                    Err(mpsc::error::TrySendError::Closed(_)) => break,
                }
            }
            Ok(None) => {}
            Err(e) => {
                warn!("Capture error: {e:#}");
                thread::sleep(Duration::from_millis(100));
                continue;
            }
        }

        let elapsed = t.elapsed();
        if elapsed < frame_dur {
            thread::sleep(frame_dur - elapsed);
        }
    }

    capture.stop();
    info!("Capture thread завершён");
}

// ─── Encode + relay loop (tokio task) ────────────────────────────────────────

async fn encode_loop(
    config:   StreamConfig,
    running:  Arc<AtomicBool>,
    stats:    Arc<StreamStats>,
    frame_rx: &mut mpsc::Receiver<CapturedFrame>,
    sender:   Arc<WebRtcSender>,
) -> Result<()> {
    let hq = HwEncoder::new(config.width, config.height, config.fps, config.bitrate)
        .context("HQ encoder")?;

    let lq = if config.simulcast {
        let (w, h) = config.lq_resolution();
        Some(LqEncoder::new(config.width, config.height, w, h, config.fps, config.lq_bitrate())
            .context("LQ encoder")?)
    } else {
        None
    };

    let mut encoders = Encoders { hq, lq };

    ipc::send_event(&Event::StreamStarted {
        encoder: encoders.hq.codec_name().to_string(),
        width:   config.width,
        height:  config.height,
        fps:     config.fps,
    })
    .await?;

    let mut fps_count = 0u32;
    let mut fps_timer = Instant::now();

    while running.load(Ordering::Relaxed) {
        let frame = match frame_rx.recv().await {
            Some(f) => f,
            None => { info!("Frame channel closed"); break; }
        };

        // ── FIX: PLI обработка ────────────────────────────────────────────────
        // Pion SFU посылает PLI когда подключается новый зритель или когда
        // зритель не может декодировать поток (нет IDR-кадра).
        // Мы проверяем флаг и форсируем keyframe, чтобы зритель получил IDR.
        if sender.take_pli_request() {
            encoders.hq.request_keyframe();
            if let Some(ref mut lq_enc) = encoders.lq {
                lq_enc.request_keyframe();
            }
            info!("[Pipeline] PLI от SFU → форсируем IDR-кадр");
        }

        // ── HQ encode → WebRTC ────────────────────────────────────────────────
        match encoders.hq.encode(&frame) {
            Ok(packets) => {
                for pkt in &packets {
                    sender.push_frame(Bytes::copy_from_slice(&pkt.data), pkt.is_key);
                }
                stats.frames_encoded.fetch_add(1, Ordering::Relaxed);
                fps_count += 1;
            }
            Err(e) => warn!("HQ encode error: {e:#}"),
        }

        // ── LQ simulcast (опционально) ────────────────────────────────────────
        if let Some(ref mut lq_enc) = encoders.lq {
            if let Err(e) = lq_enc.encode(&frame) {
                warn!("LQ encode error: {e:#}");
            }
        }

        // ── Статистика раз в секунду ──────────────────────────────────────────
        if fps_timer.elapsed() >= Duration::from_secs(1) {
            stats.last_fps.store(fps_count as u64, Ordering::Relaxed);
            let _ = ipc::send_event(&Event::Stats {
                fps:            fps_count,
                bitrate_kbps:   config.bitrate / 1000,
                encoder:        encoders.hq.codec_name().to_string(),
                dropped_frames: stats.frames_dropped.load(Ordering::Relaxed),
            })
            .await;
            fps_count = 0;
            fps_timer = Instant::now();
        }
    }

    let _ = encoders.hq.flush();
    if let Some(ref mut lq) = encoders.lq { let _ = lq.flush(); }

    info!("Encode loop завершён");
    Ok(())
}