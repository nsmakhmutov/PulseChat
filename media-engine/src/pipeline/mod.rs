// src/pipeline/mod.rs — Конвейер: WGC Capture → HW Encode → WebRTC → Pion SFU
//
// ── ИСПРАВЛЕНИЯ v0.3.1 ──────────────────────────────────────────────────────
//
//   FIX #25: restart_capture race. Раньше `running` был общий для ВСЕХ циклов
//     (capture, encode). При restart_capture() мы ставили false, ждали старый
//     capture thread, затем снова ставили true и запускали НОВЫЙ encode_loop.
//     Но старый encode_loop видел running=false и тоже завершался — ОК.
//     Проблема была в ГОНКЕ: между store(false) и store(true) + spawn нового
//     encode_loop, старый мог ещё не завершиться → два encode_loop'а одновременно
//     писали в один WebRtcSender с разными SPS/PPS/timestamps → битый H.264.
//
//     Решение: ОТДЕЛЬНЫЕ AtomicBool для capture и encode, + await завершения
//     старого encode_task через JoinHandle перед стартом нового.
//
//   FIX #34: frame buffer pool. Раньше каждый BGRA-кадр (720p × 4B = 3.7MB)
//     аллоцировался заново через `vec![0u8; ...]`. При 30 fps = 112 MB/сек
//     мусора для GC аллокатора. Теперь используем tokio::sync::mpsc с Bytes
//     (Arc<[u8]>, zero-copy clone) + capture переиспользует 2 буфера ping-pong.

use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use bytes::Bytes;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tracing::{error, info, warn};

use crate::capture::{CapturedFrame, ScreenCapture};
use crate::encode::{self, HwEncoder, LqEncoder};
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
    pub frames_skipped:  AtomicU64,
    pub last_fps:        AtomicU64,
}

impl Default for StreamStats {
    fn default() -> Self {
        Self {
            frames_captured: AtomicU64::new(0),
            frames_encoded:  AtomicU64::new(0),
            frames_dropped:  AtomicU64::new(0),
            frames_skipped:  AtomicU64::new(0),
            last_fps:        AtomicU64::new(0),
        }
    }
}

// ─── Encoders wrapper ────────────────────────────────────────────────────────

struct Encoders {
    hq: HwEncoder,
    lq: Option<LqEncoder>,
}
unsafe impl Send for Encoders {}

// ─── Pipeline ────────────────────────────────────────────────────────────────
//
// FIX #25: отдельные AtomicBool для capture и encode.
//   capture_running — управляет thread захвата (std::thread).
//   encode_running — управляет tokio-задачей энкодинга.
//   При restart_capture перезапускаем оба, но через ЯВНОЕ ожидание
//   завершения старых перед запуском новых.

pub struct Pipeline {
    capture_running: Arc<AtomicBool>,
    encode_running:  Arc<AtomicBool>,
    config:          StreamConfig,
    stats:           Arc<StreamStats>,
    webrtc_sender:   Arc<WebRtcSender>,
    target_bitrate:  Arc<AtomicU32>,
    capture_handle:  Option<thread::JoinHandle<()>>,
    encode_handle:   Option<JoinHandle<()>>,
}

impl Pipeline {
    pub async fn start(config: StreamConfig) -> Result<Self> {
        let capture_running = Arc::new(AtomicBool::new(true));
        let encode_running  = Arc::new(AtomicBool::new(true));
        let stats   = Arc::new(StreamStats::default());
        let target_bitrate = Arc::new(AtomicU32::new(config.bitrate));

        let (sender, offer_sdp) = WebRtcSender::new(config.fps)
            .await
            .context("WebRtcSender::new")?;

        ipc::send_event(&Event::WebrtcOffer { sdp: offer_sdp })
            .await
            .context("send WEBRTC_OFFER")?;

        info!(
            "Pipeline: {}×{} @ {} fps, {} kbps — ждём WEBRTC_ANSWER",
            config.width, config.height, config.fps, config.bitrate / 1000
        );

        let (frame_tx, frame_rx) = mpsc::channel::<CapturedFrame>(4);

        let cap_running = capture_running.clone();
        let cap_stats   = stats.clone();
        let cap_config  = config.clone();

        let capture_handle = thread::Builder::new()
            .name("wgc-capture".into())
            .spawn(move || {
                capture_loop(cap_config, cap_running, cap_stats, frame_tx);
            })
            .context("spawn capture thread")?;

        let enc_running = encode_running.clone();
        let enc_stats   = stats.clone();
        let enc_config  = config.clone();
        let enc_sender  = Arc::clone(&sender);
        let enc_bitrate = Arc::clone(&target_bitrate);

        let encode_handle = tokio::spawn(async move {
            if let Err(e) = encode_loop(
                enc_config, enc_running, enc_stats,
                frame_rx, enc_sender, enc_bitrate,
            ).await {
                error!("Encode loop error: {e:#}");
                let _ = ipc::send_event(&Event::Error { message: format!("{e}") }).await;
            }
        });

        Ok(Self {
            capture_running, encode_running, config, stats,
            webrtc_sender: sender, target_bitrate,
            capture_handle: Some(capture_handle),
            encode_handle:  Some(encode_handle),
        })
    }

    pub async fn set_webrtc_answer(&self, sdp: String) -> Result<()> {
        self.webrtc_sender.set_answer(sdp).await
    }

    pub fn stats(&self) -> &StreamStats { &self.stats }

    pub fn set_bitrate(&self, bitrate: u32) {
        self.target_bitrate.store(bitrate, Ordering::Relaxed);
        info!("Pipeline: ABR target → {} kbps", bitrate / 1000);
    }

    /// Перезапускает только поток захвата экрана + encode_loop без остановки WebRTC.
    ///
    /// FIX #25: правильная последовательность с раздельными AtomicBool:
    ///   1. capture_running = false → capture_thread завершится.
    ///   2. join(capture_thread) с spawn_blocking.
    ///   3. encode_running = false → старый encode_loop завершится (frame_rx закроется).
    ///   4. await(old_encode_task) — гарантированно дожидаемся завершения.
    ///   5. Создаём новые AtomicBool(true) + новый mpsc-канал.
    ///   6. Запускаем новый capture + новый encode.
    ///
    ///   Гарантия: между шагом 4 и 6 нет момента, когда два encode_loop'а
    ///   могли бы писать в один WebRtcSender одновременно.
    pub async fn restart_capture(&mut self) -> Result<()> {
        info!("[Pipeline] RESTART_CAPTURE: останавливаем старый захват...");

        // 1. Сигнализируем обоим циклам об остановке
        self.capture_running.store(false, Ordering::Release);
        self.encode_running.store(false, Ordering::Release);

        // 2. Ждём завершения capture_thread
        if let Some(h) = self.capture_handle.take() {
            tokio::task::spawn_blocking(move || {
                if h.join().is_err() {
                    warn!("[Pipeline] capture_thread join error");
                }
            })
            .await
            .ok();
        }

        // 3. Ждём завершения encode_task — критично для предотвращения
        //    одновременной работы двух encode_loop'ов на один WebRtcSender.
        if let Some(h) = self.encode_handle.take() {
            if let Err(e) = h.await {
                warn!("[Pipeline] encode_task join error: {e:?}");
            }
        }

        info!("[Pipeline] RESTART_CAPTURE: старый захват остановлен, запускаем новый...");

        // 4. Новые AtomicBool (не переиспользуем старые — безопаснее).
        self.capture_running = Arc::new(AtomicBool::new(true));
        self.encode_running  = Arc::new(AtomicBool::new(true));

        let (frame_tx, frame_rx) = mpsc::channel::<CapturedFrame>(4);

        // 5. Новый capture thread
        let cap_running = self.capture_running.clone();
        let cap_stats   = self.stats.clone();
        let cap_config  = self.config.clone();

        let capture_handle = thread::Builder::new()
            .name("wgc-capture-restarted".into())
            .spawn(move || {
                capture_loop(cap_config, cap_running, cap_stats, frame_tx);
            })
            .context("spawn restarted capture thread")?;

        self.capture_handle = Some(capture_handle);

        // 6. Новый encode_loop с тем же WebRtcSender (WebRTC-соединение живо)
        let enc_running = self.encode_running.clone();
        let enc_stats   = self.stats.clone();
        let enc_config  = self.config.clone();
        let enc_sender  = Arc::clone(&self.webrtc_sender);
        let enc_bitrate = Arc::clone(&self.target_bitrate);

        let encode_handle = tokio::spawn(async move {
            if let Err(e) = encode_loop(
                enc_config, enc_running, enc_stats,
                frame_rx, enc_sender, enc_bitrate,
            ).await {
                error!("Restarted encode loop error: {e:#}");
                let _ = ipc::send_event(&Event::Error {
                    message: format!("{e}"),
                }).await;
            }
        });

        self.encode_handle = Some(encode_handle);

        info!("[Pipeline] RESTART_CAPTURE: новый захват запущен ✓");

        ipc::send_event(&Event::CaptureRestarted {
            reason: "DLL Capture watchdog: RMS=0 timeout".to_string(),
        })
        .await
        .ok();

        Ok(())
    }

    pub async fn stop(&mut self) -> Result<()> {
        info!("Pipeline: остановка...");
        self.capture_running.store(false, Ordering::Release);
        self.encode_running.store(false, Ordering::Release);

        if let Some(h) = self.capture_handle.take() {
            let _ = tokio::task::spawn_blocking(move || { let _ = h.join(); }).await;
        }
        if let Some(h) = self.encode_handle.take() {
            let _ = h.await;
        }
        self.webrtc_sender.close().await?;
        ipc::send_event(&Event::StreamStopped).await?;
        info!("Pipeline остановлен");
        Ok(())
    }
}

impl Drop for Pipeline {
    fn drop(&mut self) {
        self.capture_running.store(false, Ordering::Release);
        self.encode_running.store(false, Ordering::Release);
    }
}

// ─── Capture loop ────────────────────────────────────────────────────────────

fn capture_loop(
    config: StreamConfig, running: Arc<AtomicBool>,
    stats: Arc<StreamStats>, frame_tx: mpsc::Sender<CapturedFrame>,
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

    // SENIOR FIX: валидация fps. Раньше Duration::from_secs_f64(1.0 / 0)
    // = inf → panic "non-finite value". FIX #35 прикрыл webrtc_out и encode,
    // но capture_loop использовал config.fps напрямую без clamp.
    // 120 fps — разумный верхний предел для экран-стрима.
    let safe_fps = config.fps.max(1).min(120);
    let frame_dur = Duration::from_secs_f64(1.0 / safe_fps as f64);

    while running.load(Ordering::Acquire) {
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
            Err(e) => { warn!("Capture error: {e:#}"); thread::sleep(Duration::from_millis(100)); continue; }
        }
        let elapsed = t.elapsed();
        if elapsed < frame_dur { thread::sleep(frame_dur - elapsed); }
    }

    capture.stop();
    info!("Capture thread завершён");
}

// ─── Encode + relay loop ─────────────────────────────────────────────────────

async fn encode_loop(
    config:   StreamConfig,
    running:  Arc<AtomicBool>,
    stats:    Arc<StreamStats>,
    mut frame_rx: mpsc::Receiver<CapturedFrame>,
    sender:   Arc<WebRtcSender>,
    target_bitrate: Arc<AtomicU32>,
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
        width: config.width, height: config.height, fps: config.fps,
    }).await?;

    let mut fps_count = 0u32;
    let mut fps_timer = Instant::now();
    let mut current_bitrate = config.bitrate;

    // FIX #34: prev_frame_data переиспользуется без аллокаций.
    // При первом кадре делаем clone() один раз, далее copy_from_slice если
    // размер совпадает.
    let mut prev_frame_data: Vec<u8> = Vec::new();
    let mut skip_count: u32 = 0;
    let max_skip = config.fps.max(1);
    let mut dedup_skipped_total: u64 = 0;

    while running.load(Ordering::Acquire) {
        let frame = match frame_rx.recv().await {
            Some(f) => f,
            None => { info!("Frame channel closed"); break; }
        };

        // ── ABR ──────────────────────────────────────────────────────────
        let new_br = target_bitrate.load(Ordering::Relaxed);
        if new_br != current_bitrate && new_br >= 500_000 {
            info!("[ABR] {} → {} kbps", current_bitrate / 1000, new_br / 1000);
            match encoders.hq.set_bitrate(new_br) {
                Ok(()) => { current_bitrate = new_br; skip_count = max_skip; }
                Err(e) => {
                    warn!("[ABR] set_bitrate failed: {e:#}");
                    target_bitrate.store(current_bitrate, Ordering::Relaxed);
                }
            }
        }

        // ── PLI → сброс dedup ────────────────────────────────────────────
        let pli = sender.take_pli_request();
        if pli {
            encoders.hq.request_keyframe();
            if let Some(ref mut lq) = encoders.lq { lq.request_keyframe(); }
            skip_count = max_skip;
            info!("[Pipeline] PLI → IDR + dedup reset");
        }

        // ── Frame Dedup ─────────────────────────────────────────────────
        let must_encode = skip_count >= max_skip
            || prev_frame_data.len() != frame.data.len()
            || encode::frame_changed(&prev_frame_data, &frame.data,
                                     frame.width, frame.height, frame.stride);

        if !must_encode {
            skip_count += 1;
            dedup_skipped_total += 1;
            stats.frames_skipped.fetch_add(1, Ordering::Relaxed);
            continue;
        }

        skip_count = 0;

        // Сохраняем prev_frame_data без лишних аллокаций
        if prev_frame_data.len() != frame.data.len() {
            prev_frame_data.clear();
            prev_frame_data.extend_from_slice(&frame.data);
        } else {
            prev_frame_data.copy_from_slice(&frame.data);
        }

        // ── HQ encode → WebRTC ──────────────────────────────────────────
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

        // ── LQ simulcast ────────────────────────────────────────────────
        if let Some(ref mut lq) = encoders.lq {
            if let Err(e) = lq.encode(&frame) { warn!("LQ encode error: {e:#}"); }
        }

        // ── Статистика 1/сек ────────────────────────────────────────────
        if fps_timer.elapsed() >= Duration::from_secs(1) {
            stats.last_fps.store(fps_count as u64, Ordering::Relaxed);
            let skipped = stats.frames_skipped.swap(0, Ordering::Relaxed);
            let _ = ipc::send_event(&Event::Stats {
                fps:          fps_count,
                bitrate_kbps: current_bitrate / 1000,
                encoder:      encoders.hq.codec_name().to_string(),
                dropped_frames: stats.frames_dropped.load(Ordering::Relaxed),
            }).await;

            if skipped > 0 {
                info!("[Dedup] Пропущено {skipped} статичных кадров (total: {dedup_skipped_total})");
            }

            fps_count = 0;
            fps_timer = Instant::now();
        }
    }

    let _ = encoders.hq.flush();
    if let Some(ref mut lq) = encoders.lq { let _ = lq.flush(); }

    info!("Encode loop завершён (dedup skipped total: {dedup_skipped_total})");
    Ok(())
}
