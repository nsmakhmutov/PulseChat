// src/ipc/mod.rs — JSON-протокол обмена с Python-хостом (stdin/stdout)
//
// ── НОВАЯ АРХИТЕКТУРА (v2) ───────────────────────────────────────────────────
//
//  WebRTC полностью УДАЛЁН из Rust. Rust занимается только:
//    1. Захват экрана (WGC / DXGI)
//    2. Аппаратное кодирование H.264 (NVENC / AMF / QSV / x264)
//    3. Отправка NAL-юнитов Python через Windows Named Pipe
//
//  Python принимает закодированные кадры через Named Pipe,
//  декодирует в av.VideoFrame и отдаёт в aiortc (VideoStreamTrack).
//  WebRTC сигнализация и DTLS — полностью на стороне Python (aiortc).
//
// ─── Python → Rust (stdin) ───────────────────────────────────────────────────
//   {"cmd": "START_STREAM", "monitor": 0, "width": 1280, "height": 720,
//    "fps": 30, "bitrate": 6000000, "simulcast": true}
//   {"cmd": "STOP_STREAM"}
//   {"cmd": "SET_BITRATE", "bitrate": 4000000, "lq_bitrate": 1000000}
//   {"cmd": "SHUTDOWN"}
//
// ─── Rust → Python (stdout) ──────────────────────────────────────────────────
//   {"event": "READY",          "version": "0.2.0"}
//   {"event": "PIPE_READY",     "pipe_name": "\\\\.\\pipe\\inpulse-me-12345"}
//   {"event": "STREAM_STARTED", "encoder": "AMD AMF", "width":1280, "height":720, "fps":30}
//   {"event": "STREAM_STOPPED"}
//   {"event": "STATS",          "fps":30, "bitrate_kbps":6000, "encoder":"AMD AMF", "dropped_frames":0}
//   {"event": "ERROR",          "message": "..."}
//
// ─── Named Pipe: формат кадра ─────────────────────────────────────────────────
//   [4 bytes LE: payload_size]  — размер H.264 данных
//   [1 byte: flags]             — bit0=is_keyframe, bit1=is_lq (LQ simulcast)
//   [3 bytes: reserved]         — зарезервировано (0x00)
//   [payload_size bytes]        — H.264 Annex B NAL units
//
// ─── Удалённые команды/события (были в v1) ────────────────────────────────────
//   VIEWER_OFFER, ICE_CANDIDATE, STREAMER_ANSWER, FORCE_KEYFRAME (команды)
//   OFFER, ANSWER, ICE_CANDIDATE (события)
//   → WebRTC теперь целиком в Python (aiortc)

use serde::{Deserialize, Serialize};

// ─── Входящие команды от Python ──────────────────────────────────────────────

#[derive(Debug, Deserialize)]
#[serde(tag = "cmd")]
pub enum Command {
    /// Запуск захвата + кодирования + Named Pipe
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

    /// Остановка стрима и закрытие Named Pipe
    #[serde(rename = "STOP_STREAM")]
    StopStream,

    /// Изменение битрейта на лету
    #[serde(rename = "SET_BITRATE")]
    SetBitrate {
        bitrate: u32,
        #[serde(default)]
        lq_bitrate: u32,
    },

    /// Корректное завершение процесса
    #[serde(rename = "SHUTDOWN")]
    Shutdown,
}

fn default_bitrate() -> u32 { 6_000_000 }

// ─── Исходящие события в Python ──────────────────────────────────────────────

#[derive(Debug, Serialize)]
#[serde(tag = "event")]
pub enum Event {
    /// Процесс запустился и готов к командам
    #[serde(rename = "READY")]
    Ready { version: String },

    /// Named Pipe создан и ждёт подключения Python
    /// Отправляется ДО STREAM_STARTED, Python должен подключиться к пайпу
    /// до того, как пойдут кадры.
    #[serde(rename = "PIPE_READY")]
    PipeReady {
        /// Имя пайпа вида "\\.\pipe\inpulse-me-{pid}"
        pipe_name: String,
    },

    /// Захват и кодирование запущены
    #[serde(rename = "STREAM_STARTED")]
    StreamStarted {
        encoder: String,
        width: u32,
        height: u32,
        fps: u32,
    },

    /// Захват остановлен
    #[serde(rename = "STREAM_STOPPED")]
    StreamStopped,

    /// Периодическая статистика (раз в секунду)
    #[serde(rename = "STATS")]
    Stats {
        fps: u32,
        bitrate_kbps: u32,
        encoder: String,
        dropped_frames: u64,
    },

    /// Ошибка (не фатальная)
    #[serde(rename = "ERROR")]
    Error { message: String },
}

// ─── Утилиты отправки ───────────────────────────────────────────────────────

use tokio::io::AsyncWriteExt;

/// Отправляет JSON-событие в stdout (одна строка + \n).
/// Python читает построчно: `for line in iter(proc.stdout.readline, b"")`
pub async fn send_event(event: &Event) -> anyhow::Result<()> {
    let json = serde_json::to_string(event)?;
    let mut stdout = tokio::io::stdout();
    stdout.write_all(json.as_bytes()).await?;
    stdout.write_all(b"\n").await?;
    stdout.flush().await?;
    Ok(())
}

/// Синхронная версия для вызова из не-async контекста (capture/encode потоки).
pub fn send_event_sync(event: &Event) {
    if let Ok(json) = serde_json::to_string(event) {
        use std::io::Write;
        let mut out = std::io::stdout().lock();
        let _ = writeln!(out, "{json}");
        let _ = out.flush();
    }
}

// ─── Формат фрейма для Named Pipe ───────────────────────────────────────────

/// Флаги для поля flags в заголовке кадра.
pub mod frame_flags {
    pub const KEYFRAME: u8 = 0x01;  // IDR-кадр (для декодера Python)
    pub const LQ:       u8 = 0x02;  // Simulcast LQ поток
}

/// Заголовок кадра (8 байт), записывается в Named Pipe перед payload.
#[repr(C)]
pub struct FrameHeader {
    pub size:     u32,  // размер payload (LE)
    pub flags:    u8,   // frame_flags::*
    pub reserved: [u8; 3],
}

impl FrameHeader {
    pub fn to_bytes(&self) -> [u8; 8] {
        let size_bytes = self.size.to_le_bytes();
        [
            size_bytes[0], size_bytes[1], size_bytes[2], size_bytes[3],
            self.flags,
            0, 0, 0,
        ]
    }
}
