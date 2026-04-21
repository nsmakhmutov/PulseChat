// main.rs — InPulse Media Engine v0.3.1
//
// v3: Rust сам делает WebRTC (webrtc-rs) прямо в Pion SFU.
//     Named Pipe удалён. Python только сигнализация.

mod capture;
mod encode;
mod ipc;
mod pipeline;
mod webrtc_out;

use anyhow::{Context, Result};
use tokio::io::{AsyncBufReadExt, BufReader};
use tracing::{error, info, warn};
use tracing_subscriber::EnvFilter;

use ipc::{Command, Event};
use pipeline::{Pipeline, StreamConfig};

const VERSION: &str = env!("CARGO_PKG_VERSION");

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .with_writer(std::io::stderr)
        .with_target(false)
        .init();

    info!("InPulse Media Engine v{VERSION} запущен");

    ipc::send_event(&Event::Ready {
        version: VERSION.to_string(),
    })
    .await?;

    let mut pipeline: Option<Pipeline> = None;
    let mut reader = BufReader::new(tokio::io::stdin());
    let mut line = String::new();

    loop {
        // FIX: очищаем буфер в НАЧАЛЕ каждой итерации. Раньше очистка была
        // внутри Ok(_)-ветки, что оставляло старые данные при Err-пути и
        // при пустых строках — потенциально приводило к накоплению.
        line.clear();
        match reader.read_line(&mut line).await {
            Ok(0) => {
                info!("stdin EOF — завершаемся");
                break;
            }
            Ok(_) => {
                let trimmed = line.trim();
                if !trimmed.is_empty() {
                    match serde_json::from_str::<Command>(trimmed) {
                        Ok(cmd) => {
                            if let Err(e) = handle_command(cmd, &mut pipeline).await {
                                error!("Ошибка команды: {e:#}");
                                let _ = ipc::send_event(&Event::Error {
                                    message: format!("{e:#}"),
                                })
                                .await;
                            }
                        }
                        Err(e) => {
                            warn!("Невалидный JSON: {e} — «{trimmed}»");
                        }
                    }
                }
            }
            Err(e) => {
                error!("stdin read error: {e}");
                break;
            }
        }
    }

    if let Some(mut p) = pipeline.take() {
        p.stop().await?;
    }

    info!("Media Engine завершён");
    Ok(())
}

async fn handle_command(cmd: Command, pipeline: &mut Option<Pipeline>) -> Result<()> {
    match cmd {
        Command::StartStream {
            monitor, width, height, fps, bitrate, stream_audio, simulcast,
        } => {
            if let Some(mut p) = pipeline.take() {
                p.stop().await?;
            }

            // FIX: валидация fps — раньше fps=0 вызывал division by zero panic
            // в Duration::from_secs_f64(1.0/0.0) = inf и в Rational::new(1, 0).
            let fps_safe = fps.max(1).min(120);
            if fps != fps_safe {
                warn!("fps={fps} некорректен, использую {fps_safe}");
            }

            let config = StreamConfig {
                monitor,
                width:  (width & !1).max(2), // выравниваем до чётного, минимум 2
                height: (height & !1).max(2),
                fps: fps_safe,
                bitrate: bitrate.max(100_000), // минимум 100 kbps
                simulcast,
                stream_audio,
            };

            info!(
                "START_STREAM: монитор={}, {}×{} @ {} fps, {} kbps",
                config.monitor, config.width, config.height,
                config.fps, config.bitrate / 1000,
            );

            let p = Pipeline::start(config).await.context("Pipeline::start")?;
            *pipeline = Some(p);
        }

        Command::StopStream => {
            if let Some(mut p) = pipeline.take() {
                p.stop().await?;
            } else {
                warn!("STOP_STREAM: стрим не запущен");
            }
        }

        Command::WebrtcAnswer { sdp } => {
            if let Some(p) = pipeline.as_ref() {
                info!("WEBRTC_ANSWER получен (len={})", sdp.len());
                p.set_webrtc_answer(sdp)
                    .await
                    .context("set_webrtc_answer")?;
            } else {
                warn!("WEBRTC_ANSWER: нет активного pipeline");
            }
        }

        Command::SetBitrate { bitrate, lq_bitrate } => {
            info!(
                "SET_BITRATE: {} kbps (LQ={} kbps)",
                bitrate / 1000, lq_bitrate / 1000
            );
            if let Some(p) = pipeline.as_ref() {
                p.set_bitrate(bitrate);
            } else {
                warn!("SET_BITRATE: нет активного pipeline");
            }
        }

        Command::RestartCapture => {
            info!("RESTART_CAPTURE: получена команда от Python watchdog");
            if let Some(p) = pipeline.as_mut() {
                if let Err(e) = p.restart_capture().await {
                    error!("RESTART_CAPTURE failed: {e:#}");
                    let _ = ipc::send_event(&Event::Error {
                        message: format!("restart_capture: {e}"),
                    })
                    .await;
                }
            } else {
                warn!("RESTART_CAPTURE: нет активного pipeline — игнорируем");
            }
        }

        Command::Shutdown => {
            info!("SHUTDOWN");
            if let Some(mut p) = pipeline.take() {
                p.stop().await?;
            }
            std::process::exit(0);
        }
    }

    Ok(())
}
