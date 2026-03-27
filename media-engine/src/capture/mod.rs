// src/capture/mod.rs — Захват экрана через Windows Graphics Capture API
//
// АРХИТЕКТУРА ZERO-COPY:
//   1. WGC захватывает кадр → ID3D11Texture2D (GPU)
//   2. Мы создаём staging texture (GPU→CPU mappable) ОДИН раз
//   3. CopyResource: source texture → staging texture (GPU-копия, быстро)
//   4. Map staging → получаем *mut u8 с BGRA данными (одна CPU-копия)
//   5. Данные уходят в энкодер (ffmpeg hw_upload или sw fallback)
//
// Для ПОЛНОГО zero-copy (без Map) нужен D3D11→NVENC interop:
//   NVENC принимает ID3D11Texture2D напрямую через NvEncRegisterResource.
//   Это реализуемо, но требует unsafe FFI к NVIDIA Video Codec SDK.
//   Текущая версия: 1 GPU-копия + 1 Map = ~0.3 мс на 1080p.

#[cfg(windows)]
pub mod wgc;

#[cfg(windows)]
pub use wgc::ScreenCapture;

/// Захваченный кадр — владеет буфером BGRA пикселей.
#[derive(Clone)]
pub struct CapturedFrame {
    /// BGRA8 пиксели (row-major, без padding между строками)
    pub data: Vec<u8>,
    pub width: u32,
    pub height: u32,
    /// Stride в байтах (может быть > width*4 из-за alignment)
    pub stride: u32,
    /// Монотонная метка времени кадра (наносекунды)
    pub timestamp_ns: u64,
}

impl CapturedFrame {
    /// Возвращает количество байт в строке без padding
    pub fn row_bytes(&self) -> u32 {
        self.width * 4
    }
}
