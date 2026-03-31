// src/ipc/mod.rs — JSON-протокол обмена с Python-хостом v3
//
// ─── Python → Rust (stdin) ───────────────────────────────────────────────────
//   {"cmd": "START_STREAM",  "monitor":0, "width":1280, "height":720, "fps":30, "bitrate":6000000}
//   {"cmd": "STOP_STREAM"}
//   {"cmd": "SET_BITRATE",   "bitrate":4000000, "lq_bitrate":1000000}
//   {"cmd": "WEBRTC_ANSWER", "sdp":"<answer SDP от Pion SFU>"}
//   {"cmd": "SHUTDOWN"}
//
// ─── Rust → Python (stdout) ──────────────────────────────────────────────────
//   {"event": "READY",          "version": "0.3.0"}
//   {"event": "WEBRTC_OFFER",   "sdp": "<offer SDP + все ICE кандидаты>"}
//   {"event": "STREAM_STARTED", "encoder":"NVENC", "width":1280, "height":720, "fps":30}
//   {"event": "STREAM_STOPPED"}
//   {"event": "STATS",          "fps":30, "bitrate_kbps":6000, "encoder":"NVENC", "dropped_frames":0}
//   {"event": "ERROR",          "message": "..."}
//
// ─── Сигнализация ────────────────────────────────────────────────────────────
//   1. START_STREAM → Rust создаёт webrtc-rs PC, собирает ICE, эмитит WEBRTC_OFFER
//   2. Python: POST /streamer/offer к Pion SFU → answer SDP
//   3. Python: WEBRTC_ANSWER → Rust stdin
//   4. Rust: set_remote_description(answer) → ICE connected → RTP → Pion SFU

use serde::{Deserialize, Serialize};

// ─── Входящие команды от Python ──────────────────────────────────────────────

#[derive(Debug, Deserialize)]
#[serde(tag = "cmd")]
pub enum Command {
    #[serde(rename = "START_STREAM")]
    StartStream {
        monitor: u32,
        width: u32,
        height: u32,
        fps: u32,
        #[serde(default = "default_bitrate")]
        bitrate: u32,
        #[serde(default)]
        stream_audio: bool,
        #[serde(default)]
        simulcast: bool,
    },

    #[serde(rename = "STOP_STREAM")]
    StopStream,

    /// SDP answer от Pion SFU — форвардится из Python после WEBRTC_OFFER
    #[serde(rename = "WEBRTC_ANSWER")]
    WebrtcAnswer { sdp: String },

    #[serde(rename = "SET_BITRATE")]
    SetBitrate {
        bitrate: u32,
        #[serde(default)]
        lq_bitrate: u32,
    },

    #[serde(rename = "SHUTDOWN")]
    Shutdown,
}

fn default_bitrate() -> u32 { 6_000_000 }

// ─── Исходящие события в Python ──────────────────────────────────────────────

#[derive(Debug, Serialize)]
#[serde(tag = "event")]
pub enum Event {
    #[serde(rename = "READY")]
    Ready { version: String },

    /// WebRTC offer SDP (gather-complete: ICE кандидаты уже внутри SDP).
    /// Python: POST .sdp к /streamer/offer → получить answer → прислать WEBRTC_ANSWER.
    #[serde(rename = "WEBRTC_OFFER")]
    WebrtcOffer { sdp: String },

    #[serde(rename = "STREAM_STARTED")]
    StreamStarted {
        encoder: String,
        width: u32,
        height: u32,
        fps: u32,
    },

    #[serde(rename = "STREAM_STOPPED")]
    StreamStopped,

    #[serde(rename = "STATS")]
    Stats {
        fps: u32,
        bitrate_kbps: u32,
        encoder: String,
        dropped_frames: u64,
    },

    #[serde(rename = "ERROR")]
    Error { message: String },
}

// ─── Утилиты отправки ───────────────────────────────────────────────────────

use tokio::io::AsyncWriteExt;

pub async fn send_event(event: &Event) -> anyhow::Result<()> {
    let json = serde_json::to_string(event)?;
    let mut stdout = tokio::io::stdout();
    stdout.write_all(json.as_bytes()).await?;
    stdout.write_all(b"\n").await?;
    stdout.flush().await?;
    Ok(())
}

pub fn send_event_sync(event: &Event) {
    if let Ok(json) = serde_json::to_string(event) {
        use std::io::Write;
        let mut out = std::io::stdout().lock();
        let _ = writeln!(out, "{json}");
        let _ = out.flush();
    }
}
