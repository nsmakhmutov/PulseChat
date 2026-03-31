// src/encode/mod.rs — H.264 аппаратное кодирование через FFmpeg
//
// Приоритет кодеков (зеркалит Python video_engine.py):
//   1. h264_nvenc  (NVIDIA GPU)
//   2. h264_amf    (AMD GPU)
//   3. h264_qsv    (Intel QuickSync)
//   4. libx264     (CPU fallback)
//
// FIX: Убраны B-кадры (bf=2) из AMD AMF и NVENC.
//   B-кадры имеют DTS ≠ PTS. webrtc-rs пакетизирует кадры с одинаковым duration,
//   не учитывая DTS-порядок B-кадров. aiortc-декодер на стороне зрителя получает
//   пакеты в DTS-порядке, но ожидает display-порядок → avcodec_send_packet() FAIL.
//   Также B-кадры усложняют присоединение к потоку: зритель может получить B-кадр
//   до IDR и никогда не восстановить декодирование.
//
// FIX: force_keyframe = true при set_bitrate() и при смене разрешения.
//   После смены качества/разрешения новый encoder должен немедленно выдать IDR,
//   иначе зритель видит «Ожидаем видео» до следующего IDR по GOP-расписанию.

use anyhow::{anyhow, Context, Result};
use tracing::{debug, info};

use crate::capture::CapturedFrame;

extern crate ffmpeg_next as ffmpeg;

use ffmpeg::codec;
use ffmpeg::format::Pixel;
use ffmpeg::software::scaling;
use ffmpeg::util::frame::video::Video as AvFrame;
use ffmpeg::{Packet, Rational};

/// Описание найденного кодека
#[derive(Debug, Clone)]
pub struct EncoderProfile {
    pub codec_name: String,
    pub display_name: String,
    pub is_hardware: bool,
}

/// Результат кодирования одного кадра — набор NAL units.
#[derive(Clone)]
pub struct EncodedPacket {
    pub data: Vec<u8>,
    pub pts: i64,
    pub dts: i64,
    pub is_key: bool,
    pub timestamp_ns: u64,
}

/// Аппаратный H.264 энкодер.
pub struct HwEncoder {
    encoder: ffmpeg::encoder::Video,
    scaler: scaling::Context,
    profile: EncoderProfile,
    frame_index: i64,
    width: u32,
    height: u32,
    fps: u32,
    bitrate: u32,
    force_keyframe: bool,
    /// Реальные размеры источника (WGC-кадра). Инициализируются нулями,
    /// обновляются при первом encode() и при смене разрешения монитора.
    /// Используются для пересоздания scaler при изменении источника.
    src_width: u32,
    src_height: u32,
}

impl HwEncoder {
    pub fn new(width: u32, height: u32, fps: u32, bitrate: u32) -> Result<Self> {
        ffmpeg::init().context("FFmpeg init")?;

        let profiles = [
            EncoderProfile {
                codec_name: "h264_nvenc".into(),
                display_name: "NVIDIA NVENC".into(),
                is_hardware: true,
            },
            EncoderProfile {
                codec_name: "h264_amf".into(),
                display_name: "AMD AMF".into(),
                is_hardware: true,
            },
            EncoderProfile {
                codec_name: "h264_qsv".into(),
                display_name: "Intel QuickSync".into(),
                is_hardware: true,
            },
            EncoderProfile {
                codec_name: "libx264".into(),
                display_name: "libx264 (CPU)".into(),
                is_hardware: false,
            },
        ];

        let mut last_error = anyhow!("Нет доступных кодеков");

        for profile in &profiles {
            match Self::try_create(profile, width, height, fps, bitrate) {
                Ok(enc) => {
                    info!(
                        "Кодек выбран: {} ({}×{} @ {} fps, {} kbps)",
                        profile.display_name, width, height, fps, bitrate / 1000
                    );
                    return Ok(enc);
                }
                Err(e) => {
                    debug!("Кодек {} недоступен: {e}", profile.codec_name);
                    last_error = e;
                }
            }
        }

        Err(last_error.context("Все кодеки недоступны"))
    }

    fn try_create(
        profile: &EncoderProfile,
        width: u32,
        height: u32,
        fps: u32,
        bitrate: u32,
    ) -> Result<Self> {
        let codec = ffmpeg::encoder::find_by_name(&profile.codec_name)
            .ok_or_else(|| anyhow!("Кодек {} не найден", profile.codec_name))?;

        let ctx = codec::context::Context::new_with_codec(codec);
        let mut video = ctx.encoder().video()?;

        video.set_width(width);
        video.set_height(height);
        video.set_format(Pixel::YUV420P);
        video.set_time_base(Rational::new(1, fps as i32));
        video.set_frame_rate(Some(Rational::new(fps as i32, 1)));
        video.set_bit_rate(bitrate as usize);
        video.set_max_bit_rate(bitrate as usize);
        video.set_gop(fps * 2); // IDR каждые 2 секунды

        let mut opts = ffmpeg::Dictionary::new();
        match profile.codec_name.as_str() {
            "h264_nvenc" => {
                opts.set("preset", "p4");
                opts.set("tune", "hq");
                opts.set("rc", "vbr");
                // cq=18 (было 20): лучше визуальное качество при низких разрешениях.
                // Разница в битрейте минимальна (статичные UI-сцены), зато текст чётче.
                opts.set("cq", "18");
                // B-кадры убраны: DTS≠PTS ломает пакетизацию в webrtc-rs.
                opts.set("bf", "0");
                opts.set("profile", "high");
                opts.set("spatial-aq", "1");
                opts.set("temporal-aq", "1");
                opts.set("aq-strength", "15"); // выше = точнее AQ на мелких деталях
                opts.set("lookahead", "8");     // короткий lookahead улучшает I-кадры
                opts.set("forced-idr", "1");
            }
            "h264_amf" => {
                opts.set("usage", "transcoding");
                opts.set("quality", "quality");
                opts.set("profile", "high");
                opts.set("rc", "vbr_peak");
                // preanalysis улучшает качество I-кадров (текст при 360/480p).
                opts.set("preanalysis", "1");
            }
            "h264_qsv" => {
                opts.set("preset", "medium");
                opts.set("profile", "high");
            }
            "libx264" => {
                opts.set("preset", "veryfast");
                opts.set("profile", "high");
                opts.set("tune", "zerolatency");
                opts.set("crf", "18"); // лучше качество текста при низких разрешениях
            }
            _ => {}
        }

        let encoder = video.open_with(opts)?;

        // Начальный scaler — заглушка 1:1. Пересоздаётся в encode() при первом кадре
        // (или при смене разрешения монитора), когда известны реальные размеры источника.
        // LANCZOS даёт значительно лучшую чёткость текста при downscale (360p/480p).
        // BILINEAR размывает мелкие буквы в кашу; LANCZOS сохраняет субпиксельные детали.
        // Цена: +~2-3% CPU на swscale, незначительно для аппаратного энкодера.
        let scaler = scaling::Context::get(
            Pixel::BGRA, width, height,
            Pixel::YUV420P, width, height,
            scaling::Flags::LANCZOS,
        )
        .context("swscale context (placeholder)")?;

        Ok(Self {
            encoder,
            scaler,
            profile: profile.clone(),
            frame_index: 0,
            width,
            height,
            fps,
            bitrate,
            force_keyframe: false,
            src_width: 0,   // 0 = «ещё не известно», обновится в encode()
            src_height: 0,
        })
    }

    pub fn encode(&mut self, frame: &CapturedFrame) -> Result<Vec<EncodedPacket>> {
        let src_w = frame.width;
        let src_h = frame.height;
        let dst_w = self.width;
        let dst_h = self.height;

        // Пересоздаём scaler если изменились размеры источника:
        //   — первый кадр (src_width == 0)
        //   — смена разрешения монитора на лету
        // Без этого scaler создан как 1:1 (width×height → width×height), а не
        // src→dst, что приводит к кропу левого края вместо масштабирования.
        if src_w != self.src_width || src_h != self.src_height {
            // LANCZOS: чёткость текста при downscale (360/480p).
            self.scaler = scaling::Context::get(
                Pixel::BGRA,    src_w, src_h,
                Pixel::YUV420P, dst_w, dst_h,
                scaling::Flags::LANCZOS,
            )
            .context("swscale context (resize)")?;
            info!(
                "Scaler обновлён: {src_w}×{src_h} → {dst_w}×{dst_h} ({}×{} kbps)",
                self.fps, self.bitrate / 1000
            );
            self.src_width  = src_w;
            self.src_height = src_h;
            // FIX: при смене разрешения форсируем IDR.
            // Иначе зритель получает P-кадры с новым разрешением без SPS/PPS контекста.
            self.force_keyframe = true;
        }

        // Копируем ВЕСЬ кадр WGC (src_w × src_h пикселей, без кропа)
        let mut bgra_frame = AvFrame::new(Pixel::BGRA, src_w, src_h);

        // FIX: get stride BEFORE data_mut to avoid borrow conflict
        let stride = bgra_frame.stride(0);
        let plane = bgra_frame.data_mut(0);

        for row in 0..src_h as usize {
            let src_start = row * frame.stride as usize;
            let src_end   = src_start + (src_w * 4) as usize;
            let dst_start = row * stride;
            let dst_end   = dst_start + (src_w * 4) as usize;
            if src_end <= frame.data.len() && dst_end <= plane.len() {
                plane[dst_start..dst_end].copy_from_slice(&frame.data[src_start..src_end]);
            }
        }

        // BGRA(src_w × src_h) → YUV420P(dst_w × dst_h) — масштабирование через swscale
        let mut yuv_frame = AvFrame::empty();
        self.scaler.run(&bgra_frame, &mut yuv_frame)?;

        yuv_frame.set_pts(Some(self.frame_index));

        if self.force_keyframe {
            yuv_frame.set_kind(ffmpeg::util::picture::Type::I);
            self.force_keyframe = false;
        }

        self.frame_index += 1;

        self.encoder.send_frame(&yuv_frame)?;
        let mut packets = Vec::new();
        let mut pkt = Packet::empty();
        while self.encoder.receive_packet(&mut pkt).is_ok() {
            packets.push(EncodedPacket {
                data: pkt.data().unwrap_or(&[]).to_vec(),
                pts: pkt.pts().unwrap_or(0),
                dts: pkt.dts().unwrap_or(0),
                is_key: pkt.is_key(),
                timestamp_ns: frame.timestamp_ns,
            });
        }

        Ok(packets)
    }

    pub fn flush(&mut self) -> Result<Vec<EncodedPacket>> {
        self.encoder.send_eof()?;
        let mut packets = Vec::new();
        let mut pkt = Packet::empty();
        while self.encoder.receive_packet(&mut pkt).is_ok() {
            packets.push(EncodedPacket {
                data: pkt.data().unwrap_or(&[]).to_vec(),
                pts: pkt.pts().unwrap_or(0),
                dts: pkt.dts().unwrap_or(0),
                is_key: pkt.is_key(),
                timestamp_ns: 0,
            });
        }
        Ok(packets)
    }

    pub fn request_keyframe(&mut self) {
        self.force_keyframe = true;
    }

    /// Изменить битрейт на лету (пересоздаёт энкодер)
    pub fn set_bitrate(&mut self, bitrate: u32) -> Result<()> {
        info!(
            "Смена битрейта: {} kbps → {} kbps",
            self.bitrate / 1000,
            bitrate / 1000
        );
        let new = Self::try_create(&self.profile, self.width, self.height, self.fps, bitrate)?;
        self.encoder = new.encoder;
        self.scaler  = new.scaler;
        self.bitrate = bitrate;
        self.frame_index = 0;
        // Сбрасываем кэш размеров источника — scaler пересоздастся в encode()
        // с правильным src→dst маппингом при следующем кадре.
        self.src_width  = 0;
        self.src_height = 0;
        // FIX: форсируем IDR после смены битрейта/качества.
        // Без этого зритель получает P-кадры от нового энкодера без SPS/PPS →
        // «Ожидаем видео» до следующего IDR по GOP-расписанию (2 секунды+).
        self.force_keyframe = true;
        Ok(())
    }

    pub fn codec_name(&self) -> &str {
        &self.profile.display_name
    }

    pub fn is_hardware(&self) -> bool {
        self.profile.is_hardware
    }
}

// ─── LQ Encoder (Simulcast) ─────────────────────────────────────────────────

pub struct LqEncoder {
    inner: HwEncoder,
    resize_scaler: scaling::Context,
    lq_width: u32,
    lq_height: u32,
}

impl LqEncoder {
    pub fn new(
        src_width: u32,
        src_height: u32,
        lq_width: u32,
        lq_height: u32,
        fps: u32,
        lq_bitrate: u32,
    ) -> Result<Self> {
        let inner = HwEncoder::new(lq_width, lq_height, fps, lq_bitrate)?;

        // LANCZOS для LQ: лучше сохраняет текст при downscale в 2x.
        // AREA хорошо работает для фото/видео, но размывает пиксели шрифтов.
        let resize_scaler = scaling::Context::get(
            Pixel::BGRA, src_width, src_height,
            Pixel::BGRA, lq_width, lq_height,
            scaling::Flags::LANCZOS,
        )
        .context("LQ resize scaler")?;

        info!(
            "LQ энкодер: {src_width}×{src_height} → {lq_width}×{lq_height}, {} kbps",
            lq_bitrate / 1000
        );

        Ok(Self { inner, resize_scaler, lq_width, lq_height })
    }

    pub fn encode(&mut self, frame: &CapturedFrame) -> Result<Vec<EncodedPacket>> {
        // 1. BGRA(src) → BGRA(lq) resize
        let mut src_frame = AvFrame::new(Pixel::BGRA, frame.width, frame.height);

        // FIX: get stride BEFORE data_mut
        let stride = src_frame.stride(0);
        let plane = src_frame.data_mut(0);

        for row in 0..frame.height as usize {
            let src_start = row * frame.stride as usize;
            let src_end = src_start + (frame.width * 4) as usize;
            let dst_start = row * stride;
            let dst_end = dst_start + (frame.width * 4) as usize;
            if src_end <= frame.data.len() && dst_end <= plane.len() {
                plane[dst_start..dst_end].copy_from_slice(&frame.data[src_start..src_end]);
            }
        }

        let mut resized = AvFrame::empty();
        self.resize_scaler.run(&src_frame, &mut resized)?;

        // 2. Extract resized BGRA into CapturedFrame
        let lq_stride = self.lq_width * 4;
        let resized_plane = resized.data(0);
        let resized_stride = resized.stride(0);
        let mut lq_data = vec![0u8; (self.lq_height * lq_stride) as usize];
        for row in 0..self.lq_height as usize {
            let src_start = row * resized_stride;
            let src_end = src_start + lq_stride as usize;
            let dst_start = row * lq_stride as usize;
            if src_end <= resized_plane.len() {
                lq_data[dst_start..dst_start + lq_stride as usize]
                    .copy_from_slice(&resized_plane[src_start..src_end]);
            }
        }

        let lq_frame = CapturedFrame {
            data: lq_data,
            width: self.lq_width,
            height: self.lq_height,
            stride: lq_stride,
            timestamp_ns: frame.timestamp_ns,
        };

        self.inner.encode(&lq_frame)
    }

    pub fn request_keyframe(&mut self) {
        self.inner.request_keyframe();
    }

    pub fn flush(&mut self) -> Result<Vec<EncodedPacket>> {
        self.inner.flush()
    }
}