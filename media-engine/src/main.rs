// main.rs — InPulse Media Engine (Rust Sidecar) v0.2.0

mod capture;
mod encode;
mod ipc;
mod pipeline;
// mod webrtc_out;  ← УДАЛЁН в v2 (WebRTC перенесён в Python/aiortc)

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
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
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

    let stdin = tokio::io::stdin();
    let mut reader = BufReader::new(stdin);
    let mut line = String::new();

    loop {
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
                                error!("Ошибка обработки команды: {e:#}");
                                let _ = ipc::send_event(&Event::Error {
                                    message: format!("{e:#}"),
                                })
                                .await;
                            }
                        }
                        Err(e) => {
                            warn!("Невалидный JSON: {e} — строка: {trimmed}");
                        }
                    }
                }
                line.clear();
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
            monitor,
            width,
            height,
            fps,
            bitrate,
            stream_audio,
            simulcast,
        } => {
            // Останавливаем предыдущий стрим если был
            if let Some(mut p) = pipeline.take() {
                p.stop().await?;
            }

            let config = StreamConfig {
                monitor,
                width: width & !1,   // выравниваем до чётного (требование энкодера)
                height: height & !1,
                fps,
                bitrate,
                simulcast,
                stream_audio,
            };

            info!(
                "START_STREAM: монитор={}, {}×{} @ {} fps, {} kbps, simulcast={}",
                config.monitor,
                config.width,
                config.height,
                config.fps,
                config.bitrate / 1000,
                config.simulcast
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

        Command::SetBitrate { bitrate, lq_bitrate } => {
            info!(
                "SET_BITRATE: HQ={} kbps, LQ={} kbps",
                bitrate / 1000,
                lq_bitrate / 1000
            );
            // TODO: передать новый битрейт в encode_loop через атомарный флаг
        }

        Command::Shutdown => {
            info!("SHUTDOWN: завершаемся...");
            if let Some(mut p) = pipeline.take() {
                p.stop().await?;
            }
            std::process::exit(0);
        }
    }

    Ok(())
}
