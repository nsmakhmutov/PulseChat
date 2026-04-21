// src/ipc/mod.rs — JSON-протокол обмена с Python-хостом v3
//
// ─── Python → Rust (stdin) ───────────────────────────────────────────────────
//   {"cmd": "START_STREAM",  "monitor":0, "width":1280, "height":720, "fps":30, "bitrate":6000000}
//   {"cmd": "STOP_STREAM"}
//   {"cmd": "SET_BITRATE",   "bitrate":4000000, "lq_bitrate":1000000}
//   {"cmd": "WEBRTC_ANSWER", "sdp":"<answer SDP от Pion SFU>"}
//   {"cmd": "RESTART_CAPTURE"}
//   {"cmd": "SHUTDOWN"}
//
// ─── Rust → Python (stdout) ──────────────────────────────────────────────────
//   {"event": "READY",          "version": "0.3.1"}
//   {"event": "WEBRTC_OFFER",   "sdp": "<offer SDP + все ICE кандидаты>"}
//   {"event": "STREAM_STARTED", "encoder":"NVENC", "width":1280, "height":720, "fps":30}
//   {"event": "STREAM_STOPPED"}
//   {"event": "STATS",          "fps":30, "bitrate_kbps":6000, "encoder":"NVENC",
//                               "dropped_frames":0, "capture_rms":0.001, "capture_peak":0.002}
//   {"event": "CAPTURE_RESTARTED", "reason":"..."}
//   {"event": "ERROR",          "message": "..."}

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

    /// Перезапустить только захват экрана без остановки WebRTC.
    ///
    /// FIX: раньше был watchdog в media_engine_bridge._read_stderr(), который
    /// парсил [DLL-DIAG] из stderr Rust-процесса. Но [DLL-DIAG] пишется в
    /// Python audio_capture.py (другой процесс!) — watchdog никогда не
    /// срабатывал. Теперь watchdog перенесён в audio_capture.py на стороне
    /// Python, где действительно доступны RMS/peak аудио.
    ///
    /// RESTART_CAPTURE может быть вызван вручную из Python через sidecar-команду.
    #[serde(rename = "RESTART_CAPTURE")]
    RestartCapture,

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

    /// DLL Capture был перезапущен по команде RESTART_CAPTURE.
    #[serde(rename = "CAPTURE_RESTARTED")]
    CaptureRestarted { reason: String },

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
