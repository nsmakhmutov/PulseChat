// src/pipeline/mod.rs — Конвейер: WGC Capture → HW Encode → Named Pipe
use std::io::Write;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicU32, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use tokio::sync::mpsc;
use tracing::{error, info, warn};

use crate::capture::{CapturedFrame, ScreenCapture};
use crate::encode::{HwEncoder, LqEncoder};
use crate::ipc::{self, Event, FrameHeader, frame_flags};

/// Глобальный счётчик для уникальных имён пайпов (защита от ошибки 231)
static PIPE_SEQ: AtomicU32 = AtomicU32::new(0);

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

struct Encoders {
    hq: HwEncoder,
    lq: Option<LqEncoder>,
}
unsafe impl Send for Encoders {}

struct PipeHandle(windows_sys::Win32::Foundation::HANDLE);
unsafe impl Send for PipeHandle {}

impl Drop for PipeHandle {
    fn drop(&mut self) {
        unsafe {
            windows_sys::Win32::Foundation::CloseHandle(self.0);
        }
    }
}

pub struct Pipeline {
    running:        Arc<AtomicBool>,
    config:         StreamConfig,
    stats:          Arc<StreamStats>,
    capture_handle: Option<thread::JoinHandle<()>>,
}

impl Pipeline {
    pub async fn start(config: StreamConfig) -> Result<Self> {
        let running = Arc::new(AtomicBool::new(true));
        let stats   = Arc::new(StreamStats::default());

        // Уникальное имя пайпа для каждого запуска стрима
        let pid       = std::process::id();
        let seq       = PIPE_SEQ.fetch_add(1, Ordering::Relaxed);
        let pipe_name = format!(r"\\.\pipe\inpulse-me-{}-{}", pid, seq);

        let pipe_handle = Self::create_named_pipe(&pipe_name)
            .context("Создание Named Pipe")?;

        ipc::send_event_sync(&Event::PipeReady {
            pipe_name: pipe_name.clone(),
        });

        info!("Named Pipe создан: {}", pipe_name);

        let (frame_tx, mut frame_rx) = mpsc::channel::<CapturedFrame>(4);

        let capture_running = running.clone();
        let capture_stats   = stats.clone();
        let capture_config  = config.clone();

        let capture_handle = thread::Builder::new()
            .name("wgc-capture".into())
            .spawn(move || {
                Self::capture_loop(capture_config, capture_running, capture_stats, frame_tx);
            })
            .context("Spawn capture thread")?;

        let encode_running = running.clone();
        let encode_stats   = stats.clone();
        let encode_config  = config.clone();

        tokio::spawn(async move {
            let raw_handle  = pipe_handle.0;
            let pipe_name_for_log = pipe_name.clone();

            info!("Ожидание подключения Python к Named Pipe...");

            let connect_result = tokio::task::spawn_blocking(move || unsafe {
                windows_sys::Win32::System::Pipes::ConnectNamedPipe(
                    raw_handle,
                    std::ptr::null_mut(),
                )
            })
            .await
            .unwrap_or(0i32);

            if connect_result == 0 {
                let err = unsafe { windows_sys::Win32::Foundation::GetLastError() };
                if err != 535 { // ERROR_PIPE_CONNECTED
                    error!("ConnectNamedPipe error: {}", err);
                    let _ = ipc::send_event(&Event::Error {
                        message: format!("Named Pipe connect error: {err}"),
                    })
                    .await;
                    return;
                }
            }

            info!("Python подключился к Named Pipe: {}", pipe_name_for_log);

            let pipe_file = unsafe {
                use std::os::windows::io::FromRawHandle;
                std::fs::File::from_raw_handle(pipe_handle.0 as _)
            };
            std::mem::forget(pipe_handle);

            if let Err(e) = Self::encode_loop(
                encode_config,
                encode_running,
                encode_stats,
                &mut frame_rx,
                pipe_file,
            )
            .await
            {
                error!("Encode loop error: {e:#}");
                let _ = ipc::send_event(&Event::Error {
                    message: format!("Encode error: {e}"),
                })
                .await;
            }
        });

        info!(
            "Pipeline запущен: {}×{} @ {} fps, simulcast={}",
            config.width, config.height, config.fps, config.simulcast
        );

        Ok(Self {
            running,
            config,
            stats,
            capture_handle: Some(capture_handle),
        })
    }

    fn create_named_pipe(pipe_name: &str) -> Result<PipeHandle> {
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_FLAG_FIRST_PIPE_INSTANCE, PIPE_ACCESS_OUTBOUND,
        };
        use windows_sys::Win32::System::Pipes::{
            CreateNamedPipeW, PIPE_TYPE_BYTE, PIPE_WAIT,
        };
        use windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE;

        let name_wide: Vec<u16> = pipe_name
            .encode_utf16()
            .chain(std::iter::once(0))
            .collect();

        let handle = unsafe {
            CreateNamedPipeW(
                name_wide.as_ptr(),
                PIPE_ACCESS_OUTBOUND | FILE_FLAG_FIRST_PIPE_INSTANCE,
                PIPE_TYPE_BYTE | PIPE_WAIT,
                1,
                4 * 1024 * 1024,
                0,
                0,
                std::ptr::null(),
            )
        };

        if handle == INVALID_HANDLE_VALUE {
            let err = unsafe { windows_sys::Win32::Foundation::GetLastError() };
            anyhow::bail!("CreateNamedPipeW failed: error code {}", err);
        }

        Ok(PipeHandle(handle))
    }

    fn capture_loop(
        config: StreamConfig,
        running: Arc<AtomicBool>,
        stats: Arc<StreamStats>,
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
                error!("Ошибка инициализации WGC: {e:#}");
                ipc::send_event_sync(&Event::Error {
                    message: format!("WGC init error: {e}"),
                });
                return;
            }
        };

        let frame_duration = Duration::from_secs_f64(1.0 / config.fps as f64);

        while running.load(Ordering::Relaxed) {
            let t_start = Instant::now();

            match capture.grab() {
                Ok(Some(frame)) => {
                    stats.frames_captured.fetch_add(1, Ordering::Relaxed);
                    match frame_tx.try_send(frame) {
                        Ok(()) => {}
                        Err(mpsc::error::TrySendError::Full(_)) => {
                            stats.frames_dropped.fetch_add(1, Ordering::Relaxed);
                        }
                        Err(mpsc::error::TrySendError::Closed(_)) => {
                            info!("Frame channel закрыт");
                            break;
                        }
                    }
                }
                Ok(None) => {}
                Err(e) => {
                    warn!("Ошибка захвата: {e:#}");
                    thread::sleep(Duration::from_millis(100));
                    continue;
                }
            }

            let elapsed = t_start.elapsed();
            if elapsed < frame_duration {
                thread::sleep(frame_duration - elapsed);
            }
        }

        capture.stop();
        info!("Capture thread завершён");
    }

    async fn encode_loop(
        config: StreamConfig,
        running: Arc<AtomicBool>,
        stats: Arc<StreamStats>,
        frame_rx: &mut mpsc::Receiver<CapturedFrame>,
        mut pipe: std::fs::File,
    ) -> Result<()> {
        let hq_encoder =
            HwEncoder::new(config.width, config.height, config.fps, config.bitrate)
                .context("HQ encoder")?;

        let lq_encoder = if config.simulcast {
            let (lq_w, lq_h) = config.lq_resolution();
            Some(
                LqEncoder::new(
                    config.width,  config.height,
                    lq_w,          lq_h,
                    config.fps,
                    config.lq_bitrate(),
                )
                .context("LQ encoder")?,
            )
        } else {
            None
        };

        let mut encoders = Encoders {
            hq: hq_encoder,
            lq: lq_encoder,
        };

        ipc::send_event(&Event::StreamStarted {
            encoder: encoders.hq.codec_name().to_string(),
            width:   config.width,
            height:  config.height,
            fps:     config.fps,
        })
        .await?;

        let mut fps_counter = 0u32;
        let mut fps_timer   = Instant::now();

        while running.load(Ordering::Relaxed) {
            let frame = match frame_rx.recv().await {
                Some(f) => f,
                None => {
                    info!("Frame channel закрыт — encode loop завершается");
                    break;
                }
            };

            match encoders.hq.encode(&frame) {
                Ok(packets) => {
                    for pkt in &packets {
                        if let Err(e) = write_frame(&mut pipe, &pkt.data, pkt.is_key, false) {
                            warn!("HQ pipe write error: {e}");
                            running.store(false, Ordering::Relaxed);
                            break;
                        }
                    }
                    stats.frames_encoded.fetch_add(1, Ordering::Relaxed);
                    fps_counter += 1;
                }
                Err(e) => {
                    warn!("HQ encode error: {e:#}");
                }
            }

            if !running.load(Ordering::Relaxed) {
                break;
            }

            if let Some(ref mut lq) = encoders.lq {
                match lq.encode(&frame) {
                    Ok(packets) => {
                        for pkt in &packets {
                            if let Err(e) = write_frame(&mut pipe, &pkt.data, pkt.is_key, true) {
                                warn!("LQ pipe write error: {e}");
                            }
                        }
                    }
                    Err(e) => {
                        warn!("LQ encode error: {e:#}");
                    }
                }
            }

            if fps_timer.elapsed() >= Duration::from_secs(1) {
                stats.last_fps.store(fps_counter as u64, Ordering::Relaxed);

                let _ = ipc::send_event(&Event::Stats {
                    fps:            fps_counter,
                    bitrate_kbps:   config.bitrate / 1000,
                    encoder:        encoders.hq.codec_name().to_string(),
                    dropped_frames: stats.frames_dropped.load(Ordering::Relaxed),
                })
                .await;

                fps_counter = 0;
                fps_timer   = Instant::now();
            }
        }

        let _ = encoders.hq.flush();
        if let Some(ref mut lq) = encoders.lq {
            let _ = lq.flush();
        }

        info!("Encode loop завершён");
        Ok(())
    }

    pub fn stats(&self) -> &StreamStats {
        &self.stats
    }

    pub async fn stop(&mut self) -> Result<()> {
        info!("Pipeline: остановка...");
        self.running.store(false, Ordering::Relaxed);

        if let Some(handle) = self.capture_handle.take() {
            let _ = handle.join();
        }

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

fn write_frame(
    pipe: &mut impl Write,
    nal_data: &[u8],
    is_keyframe: bool,
    is_lq: bool,
) -> anyhow::Result<()> {
    let mut flags = 0u8;
    if is_keyframe { flags |= frame_flags::KEYFRAME; }
    if is_lq       { flags |= frame_flags::LQ; }

    let header = FrameHeader {
        size:     nal_data.len() as u32,
        flags,
        reserved: [0; 3],
    };

    pipe.write_all(&header.to_bytes())?;
    pipe.write_all(nal_data)?;
    pipe.flush()?;
    Ok(())
}