// main.rs — InPulse Media Engine v0.3.0
//
// v3: Rust сам делает WebRTC (webrtc-rs) прямо в Pion SFU.
//     Named Pipe удалён. Python только сигнализация.
//
// Протокол запуска:
//   1. Rust → stdout: {"event":"READY","version":"0.3.0"}
//   2. Python → stdin: {"cmd":"START_STREAM",...}
//   3. Rust → stdout: {"event":"WEBRTC_OFFER","sdp":"..."}
//   4. Python: POST sdp к Pion SFU → answer
//   5. Python → stdin: {"cmd":"WEBRTC_ANSWER","sdp":"<answer>"}
//   6. RTP кадры текут в Pion SFU
//   7. Python → stdin: {"cmd":"STOP_STREAM"} / {"cmd":"SHUTDOWN"}

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
            monitor, width, height, fps, bitrate, stream_audio, simulcast,
        } => {
            if let Some(mut p) = pipeline.take() {
                p.stop().await?;
            }

            let config = StreamConfig {
                monitor,
                width:  width  & !1, // выравниваем до чётного
                height: height & !1,
                fps,
                bitrate,
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

        // Python форвардит SDP answer от Pion SFU обратно нам
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
            // TODO: передать через AtomicU32 в encode_loop
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
