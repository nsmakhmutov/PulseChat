// src/encode/mod.rs — H.264 аппаратное кодирование через FFmpeg
//
// ── ОПТИМИЗАЦИИ ДЛЯ 360p/480p ───────────────────────────────────────────────
//
//   1. VBR+CQ вместо CBR для ≤480p:
//      Статика → ~200 kbps. Движение → до maxrate. Перцепция ЗНАЧИТЕЛЬНО лучше.
//
//   2. Y-plane Unsharp Mask после swscale (только для ≤480p):
//      Текст на 360p читается как на 480p. Стоимость: ~0.1ms.
//
//   3. Resolution-adaptive: aq-strength, lookahead, GOP.
//
//   4. frame_changed() — быстрое сравнение для frame dedup в pipeline.

use anyhow::{anyhow, Context, Result};
use tracing::{debug, info};

use crate::capture::CapturedFrame;

extern crate ffmpeg_next as ffmpeg;

use ffmpeg::codec;
use ffmpeg::format::Pixel;
use ffmpeg::software::scaling;
use ffmpeg::util::frame::video::Video as AvFrame;
use ffmpeg::{Packet, Rational};

#[derive(Debug, Clone)]
pub struct EncoderProfile {
    pub codec_name: String,
    pub display_name: String,
    pub is_hardware: bool,
}

#[derive(Clone)]
pub struct EncodedPacket {
    pub data: Vec<u8>,
    pub pts: i64,
    pub dts: i64,
    pub is_key: bool,
    pub timestamp_ns: u64,
}

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
    src_width: u32,
    src_height: u32,
    is_low_res: bool,
    sharpen_strength: i16,
}

impl HwEncoder {
    pub fn new(width: u32, height: u32, fps: u32, bitrate: u32) -> Result<Self> {
        ffmpeg::init().context("FFmpeg init")?;

        let profiles = [
            EncoderProfile { codec_name: "h264_nvenc".into(), display_name: "NVIDIA NVENC".into(), is_hardware: true },
            EncoderProfile { codec_name: "h264_amf".into(),   display_name: "AMD AMF".into(),     is_hardware: true },
            EncoderProfile { codec_name: "h264_qsv".into(),   display_name: "Intel QuickSync".into(), is_hardware: true },
            EncoderProfile { codec_name: "libx264".into(),    display_name: "libx264 (CPU)".into(),   is_hardware: false },
        ];

        let mut last_error = anyhow!("Нет доступных кодеков");
        for profile in &profiles {
            match Self::try_create(profile, width, height, fps, bitrate) {
                Ok(enc) => {
                    info!("Кодек: {} {}×{} @{} fps {} kbps [{}]",
                        profile.display_name, width, height, fps, bitrate / 1000,
                        if enc.is_low_res { "CQ+sharpen" } else { "CBR" });
                    return Ok(enc);
                }
                Err(e) => { debug!("{} недоступен: {e}", profile.codec_name); last_error = e; }
            }
        }
        Err(last_error.context("Все кодеки недоступны"))
    }

    fn try_create(profile: &EncoderProfile, width: u32, height: u32, fps: u32, bitrate: u32) -> Result<Self> {
        let codec = ffmpeg::encoder::find_by_name(&profile.codec_name)
            .ok_or_else(|| anyhow!("Кодек {} не найден", profile.codec_name))?;

        let ctx = codec::context::Context::new_with_codec(codec);
        let mut video = ctx.encoder().video()?;

        let is_low_res = height <= 480;

        video.set_width(width);
        video.set_height(height);
        video.set_format(Pixel::YUV420P);
        video.set_time_base(Rational::new(1, fps as i32));
        video.set_frame_rate(Some(Rational::new(fps as i32, 1)));
        video.set_bit_rate(bitrate as usize);
        video.set_max_bit_rate(bitrate as usize);

        // GOP: ≤480p → 1 сек (быстрое восстановление), >480p → 2 сек
        video.set_gop(if is_low_res { fps } else { fps * 2 });

        let mut opts = ffmpeg::Dictionary::new();
        match profile.codec_name.as_str() {
            "h264_nvenc" => {
                opts.set("preset", "p4");
                opts.set("tune", "ull");
                opts.set("forced-idr", "1");
                opts.set("bf", "0");
                opts.set("profile", "high");

                if is_low_res {
                    // ── ТРЮК 2: VBR + CQ — магия для низких разрешений ──────
                    // Статичный UI → ~200 kbps (CBR слал бы 1.5 Mbps впустую).
                    // Сэкономленные биты → I-кадры и моменты движения.
                    let cq = if height <= 360 { "24" } else { "22" };
                    opts.set("rc", "vbr");
                    opts.set("cq", cq);
                    opts.set("maxrate", &bitrate.to_string());
                    opts.set("bufsize", &(bitrate * 4).to_string());
                    opts.set("spatial-aq", "1");
                    opts.set("temporal-aq", "1");
                    opts.set("aq-strength", "8");
                    opts.set("lookahead", "4");
                } else {
                    opts.set("rc", "cbr");
                    opts.set("bufsize", &(bitrate * 2).to_string());
                    opts.set("maxrate", &bitrate.to_string());
                    opts.set("spatial-aq", "1");
                    opts.set("temporal-aq", "1");
                    opts.set("aq-strength", "15");
                    opts.set("lookahead", "8");
                }
            }
            "h264_amf" => {
                opts.set("usage", "transcoding");
                opts.set("quality", "quality");
                opts.set("profile", "high");
                opts.set("rc", "vbr_peak");
                opts.set("preanalysis", "1");
            }
            "h264_qsv" => {
                opts.set("preset", "medium");
                opts.set("profile", "high");
            }
            "libx264" => {
                opts.set("profile", "high");
                opts.set("tune", "zerolatency");
                opts.set("preset", "veryfast");
                let crf = if is_low_res { if height <= 360 { "24" } else { "22" } } else { "18" };
                opts.set("crf", crf);
            }
            _ => {}
        }

        let encoder = video.open_with(opts)?;

        let scaler = scaling::Context::get(
            Pixel::BGRA, width, height,
            Pixel::YUV420P, width, height,
            scaling::Flags::LANCZOS,
        ).context("swscale context")?;

        let sharpen_strength = if !is_low_res { 0 } else if height <= 360 { 5 } else { 3 };

        Ok(Self {
            encoder, scaler, profile: profile.clone(),
            frame_index: 0, width, height, fps, bitrate,
            force_keyframe: false, src_width: 0, src_height: 0,
            is_low_res, sharpen_strength,
        })
    }

    pub fn encode(&mut self, frame: &CapturedFrame) -> Result<Vec<EncodedPacket>> {
        let (src_w, src_h, dst_w, dst_h) = (frame.width, frame.height, self.width, self.height);

        if src_w != self.src_width || src_h != self.src_height {
            self.scaler = scaling::Context::get(
                Pixel::BGRA, src_w, src_h, Pixel::YUV420P, dst_w, dst_h,
                scaling::Flags::LANCZOS,
            ).context("swscale resize")?;
            info!("Scaler: {src_w}×{src_h} → {dst_w}×{dst_h}");
            self.src_width = src_w;
            self.src_height = src_h;
            self.force_keyframe = true;
        }

        let mut bgra_frame = AvFrame::new(Pixel::BGRA, src_w, src_h);
        let stride = bgra_frame.stride(0);
        let plane = bgra_frame.data_mut(0);
        for row in 0..src_h as usize {
            let ss = row * frame.stride as usize;
            let se = ss + (src_w * 4) as usize;
            let ds = row * stride;
            let de = ds + (src_w * 4) as usize;
            if se <= frame.data.len() && de <= plane.len() {
                plane[ds..de].copy_from_slice(&frame.data[ss..se]);
            }
        }

        let mut yuv_frame = AvFrame::empty();
        self.scaler.run(&bgra_frame, &mut yuv_frame)?;

        // ── ТРЮК 3: Y-plane Unsharp Mask (только ≤480p) ─────────────────
        if self.sharpen_strength > 0 {
            Self::sharpen_y_plane(&mut yuv_frame, dst_w, dst_h, self.sharpen_strength);
        }

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
                pts: pkt.pts().unwrap_or(0), dts: pkt.dts().unwrap_or(0),
                is_key: pkt.is_key(), timestamp_ns: frame.timestamp_ns,
            });
        }
        Ok(packets)
    }

    /// Y-plane Unsharp Mask (4-connected Laplacian), in-place.
    /// Ядро:  [0,-s,0] [-s,1+4s,-s] [0,-s,0],  s = strength/10.
    /// Обрабатывает только Y (яркость). Хрома U/V не тронуты → 0 лишних бит.
    fn sharpen_y_plane(frame: &mut AvFrame, w: u32, h: u32, strength: i16) {
        let y_stride = frame.stride(0);
        let y_plane  = frame.data_mut(0);
        let (w, h) = (w as usize, h as usize);

        // prev_row хранит ОРИГИНАЛЬНЫЕ значения, чтобы sharpening
        // не читал уже изменённые пиксели (cascade artifact).
        let mut prev_row = vec![0u8; w];
        prev_row[..w].copy_from_slice(&y_plane[..w]);

        for y in 1..h - 1 {
            let row_off   = y * y_stride;
            let below_off = (y + 1) * y_stride;

            // Копия текущей строки ДО модификации
            let mut curr_orig = vec![0u8; w];
            curr_orig.copy_from_slice(&y_plane[row_off..row_off + w]);

            for x in 1..w - 1 {
                let c = curr_orig[x] as i16;
                let blur = (prev_row[x] as i16
                          + y_plane[below_off + x] as i16
                          + curr_orig[x - 1] as i16
                          + curr_orig[x + 1] as i16) >> 2;
                let sharp = c + (c - blur) * strength / 10;
                y_plane[row_off + x] = sharp.clamp(0, 255) as u8;
            }
            prev_row[..w].copy_from_slice(&curr_orig);
        }
    }

    pub fn flush(&mut self) -> Result<Vec<EncodedPacket>> {
        self.encoder.send_eof()?;
        let mut out = Vec::new();
        let mut pkt = Packet::empty();
        while self.encoder.receive_packet(&mut pkt).is_ok() {
            out.push(EncodedPacket {
                data: pkt.data().unwrap_or(&[]).to_vec(),
                pts: pkt.pts().unwrap_or(0), dts: pkt.dts().unwrap_or(0),
                is_key: pkt.is_key(), timestamp_ns: 0,
            });
        }
        Ok(out)
    }

    pub fn request_keyframe(&mut self) { self.force_keyframe = true; }

    pub fn set_bitrate(&mut self, bitrate: u32) -> Result<()> {
        info!("Bitrate: {} → {} kbps", self.bitrate / 1000, bitrate / 1000);
        let new = Self::try_create(&self.profile, self.width, self.height, self.fps, bitrate)?;
        self.encoder = new.encoder;
        self.scaler = new.scaler;
        self.bitrate = bitrate;
        self.frame_index = 0;
        self.src_width = 0;
        self.src_height = 0;
        self.force_keyframe = true;
        Ok(())
    }

    pub fn codec_name(&self) -> &str { &self.profile.display_name }
    pub fn is_hardware(&self) -> bool { self.profile.is_hardware }
}

// ─── Frame Dedup ─────────────────────────────────────────────────────────────
/// Сравнивает два BGRA-кадра по сэмплированным пикселям.
/// true = кадр изменился, нужно кодировать.
/// false = статичен, можно пропустить encode.
pub fn frame_changed(prev: &[u8], curr: &[u8], width: u32, height: u32, stride: u32) -> bool {
    let total = (width * height) as usize;
    let step: usize = 16;
    let thresh: u8 = 8;
    let limit = total / step / 300; // 0.3% порог

    let (w, s) = (width as usize, stride as usize);
    let mut changed: usize = 0;
    let mut i: usize = 0;
    while i < total {
        let off = (i / w) * s + (i % w) * 4;
        if off + 2 < curr.len() && off + 2 < prev.len() {
            if curr[off].abs_diff(prev[off]) > thresh
            || curr[off+1].abs_diff(prev[off+1]) > thresh
            || curr[off+2].abs_diff(prev[off+2]) > thresh {
                changed += 1;
                if changed > limit { return true; }
            }
        }
        i += step;
    }
    false
}

// ─── LQ Encoder (Simulcast) ─────────────────────────────────────────────────
pub struct LqEncoder { inner: HwEncoder, resize_scaler: scaling::Context, lq_width: u32, lq_height: u32 }

impl LqEncoder {
    pub fn new(src_w: u32, src_h: u32, lq_w: u32, lq_h: u32, fps: u32, br: u32) -> Result<Self> {
        let inner = HwEncoder::new(lq_w, lq_h, fps, br)?;
        let resize_scaler = scaling::Context::get(Pixel::BGRA, src_w, src_h, Pixel::BGRA, lq_w, lq_h, scaling::Flags::LANCZOS).context("LQ scaler")?;
        Ok(Self { inner, resize_scaler, lq_width: lq_w, lq_height: lq_h })
    }

    pub fn encode(&mut self, frame: &CapturedFrame) -> Result<Vec<EncodedPacket>> {
        let mut src = AvFrame::new(Pixel::BGRA, frame.width, frame.height);
        let stride = src.stride(0); let plane = src.data_mut(0);
        for row in 0..frame.height as usize {
            let ss = row * frame.stride as usize; let se = ss + (frame.width*4) as usize;
            let ds = row * stride; let de = ds + (frame.width*4) as usize;
            if se <= frame.data.len() && de <= plane.len() { plane[ds..de].copy_from_slice(&frame.data[ss..se]); }
        }
        let mut resized = AvFrame::empty();
        self.resize_scaler.run(&src, &mut resized)?;
        let ls = self.lq_width * 4; let rp = resized.data(0); let rs = resized.stride(0);
        let mut ld = vec![0u8; (self.lq_height * ls) as usize];
        for row in 0..self.lq_height as usize {
            let ss = row * rs; let se = ss + ls as usize; let ds = row * ls as usize;
            if se <= rp.len() { ld[ds..ds+ls as usize].copy_from_slice(&rp[ss..se]); }
        }
        self.inner.encode(&CapturedFrame { data: ld, width: self.lq_width, height: self.lq_height, stride: ls, timestamp_ns: frame.timestamp_ns })
    }

    pub fn request_keyframe(&mut self) { self.inner.request_keyframe(); }
    pub fn flush(&mut self) -> Result<Vec<EncodedPacket>> { self.inner.flush() }
}
