// src/encode/mod.rs — H.264 аппаратное кодирование через FFmpeg
//
// ── ОПТИМИЗАЦИИ v0.3.1 ─────────────────────────────────────────────────────
//
//   FIX #34 (части):
//     1. BGRA AvFrame переиспользуется вместо AvFrame::new() на каждый кадр.
//        Раньше: 30 fps × 3.7MB = 110 MB/сек аллокации мусора.
//        Стало: одна аллокация на всю жизнь encoder'а, переиспользуется.
//     2. sharpen_y_plane buffers (prev_row, curr_orig) — предаллоцированы
//        как поля структуры, не аллоцируются на каждую строку × каждый кадр.
//        Раньше: 720p × 30 fps = 21600 allocs/sec.
//        Стало: 2 Vec<u8> аллокации за всю жизнь.
//
//   FIX #35: защита от fps=0. Rational::new(1, 0) → FFmpeg panic.
//     Теперь валидируем fps >= 1 при создании энкодера.

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

    // FIX #34: переиспользуемые буферы для sharpen (избегаем аллокаций на hot path)
    sharpen_prev_row: Vec<u8>,
    sharpen_curr_row: Vec<u8>,

    // FIX #34: переиспользуемый BGRA AvFrame
    bgra_frame: Option<AvFrame>,
    bgra_frame_dims: (u32, u32),
}

impl HwEncoder {
    pub fn new(width: u32, height: u32, fps: u32, bitrate: u32) -> Result<Self> {
        ffmpeg::init().context("FFmpeg init")?;

        // FIX #35: валидация fps
        let fps = fps.max(1);

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

        video.set_gop(if is_low_res { fps } else { fps * 2 });

        let mut opts = ffmpeg::Dictionary::new();
        match profile.codec_name.as_str() {

            // ── NVENC ────────────────────────────────────────────────────────
            "h264_nvenc" => {
                opts.set("preset",      "p1");
                opts.set("tune",        "ull");
                opts.set("zerolatency", "1");
                opts.set("forced-idr",  "1");
                opts.set("bf",          "0");
                opts.set("profile",     "high");
                opts.set("rc-lookahead","0");
                opts.set("no-scenecut", "1");

                if is_low_res {
                    let cq = if height <= 360 { "24" } else { "22" };
                    opts.set("rc",         "vbr");
                    opts.set("cq",         cq);
                    opts.set("maxrate",    &bitrate.to_string());
                    opts.set("bufsize",    &(bitrate * 2).to_string());
                    opts.set("spatial-aq", "1");
                    opts.set("temporal-aq","1");
                    opts.set("aq-strength","8");
                } else {
                    opts.set("rc",         "cbr");
                    opts.set("bufsize",    &bitrate.to_string());
                    opts.set("maxrate",    &bitrate.to_string());
                    opts.set("spatial-aq", "1");
                    opts.set("temporal-aq","1");
                    opts.set("aq-strength","8");
                }
            }

            // ── AMF ──────────────────────────────────────────────────────────
            "h264_amf" => {
                opts.set("usage",        "ultralowlatency");
                opts.set("quality",      "speed");
                opts.set("profile",      "high");
                opts.set("rc",           "cbr");
                opts.set("enforce_hrd",  "1");
                opts.set("filler_data",  "1");
                opts.set("max_b_frames", "0");
                opts.set("bf_ref",       "0");
                opts.set("vbaq",         "1");
            }

            // ── QSV ──────────────────────────────────────────────────────────
            "h264_qsv" => {
                opts.set("preset",      "veryfast");
                opts.set("profile",     "high");
                opts.set("low_power",   "1");
                opts.set("async_depth", "1");
                opts.set("bf",          "0");
            }

            // ── libx264 ──────────────────────────────────────────────────────
            "libx264" => {
                opts.set("profile",  "high");
                opts.set("tune",     "zerolatency");
                opts.set("preset",   "ultrafast");

                let br_kbps  = bitrate / 1000;
                let buf_kbps = br_kbps * 2;
                opts.set("x264opts", &format!(
                    "nal-hrd=cbr:force-cfr=1:rc-lookahead=0:\
                     scenecut=0:bframes=0:ref=1:\
                     vbv-maxrate={br_kbps}:vbv-bufsize={buf_kbps}"
                ));
                opts.set("b",       &bitrate.to_string());
                opts.set("maxrate", &bitrate.to_string());
                opts.set("bufsize", &(bitrate * 2).to_string());
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

        // Предаллоцируем буферы для sharpen (максимальная ширина = width энкодера)
        let sharpen_prev_row = if sharpen_strength > 0 { vec![0u8; width as usize] } else { Vec::new() };
        let sharpen_curr_row = if sharpen_strength > 0 { vec![0u8; width as usize] } else { Vec::new() };

        Ok(Self {
            encoder, scaler, profile: profile.clone(),
            frame_index: 0, width, height, fps, bitrate,
            force_keyframe: false, src_width: 0, src_height: 0,
            is_low_res, sharpen_strength,
            sharpen_prev_row, sharpen_curr_row,
            bgra_frame: None, bgra_frame_dims: (0, 0),
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

        // FIX #34: переиспользуем BGRA AvFrame если размер не изменился
        if self.bgra_frame.is_none() || self.bgra_frame_dims != (src_w, src_h) {
            self.bgra_frame = Some(AvFrame::new(Pixel::BGRA, src_w, src_h));
            self.bgra_frame_dims = (src_w, src_h);
        }

        let bgra_frame = self.bgra_frame.as_mut().unwrap();
        {
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
        }

        let mut yuv_frame = AvFrame::empty();
        self.scaler.run(bgra_frame, &mut yuv_frame)?;

        if self.sharpen_strength > 0 {
            Self::sharpen_y_plane_inplace(
                &mut yuv_frame, dst_w, dst_h, self.sharpen_strength,
                &mut self.sharpen_prev_row, &mut self.sharpen_curr_row,
            );
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

    /// FIX #34: Y-plane Unsharp Mask БЕЗ аллокаций — буферы prev_row/curr_row
    /// передаются извне (из self) и переиспользуются между вызовами.
    fn sharpen_y_plane_inplace(
        frame: &mut AvFrame, w: u32, h: u32, strength: i16,
        prev_row: &mut Vec<u8>, curr_orig: &mut Vec<u8>,
    ) {
        let y_stride = frame.stride(0);
        let y_plane  = frame.data_mut(0);
        let (w, h) = (w as usize, h as usize);

        // Подготовим буферы нужного размера (resize никогда не уменьшает capacity)
        if prev_row.len() < w { prev_row.resize(w, 0); }
        if curr_orig.len() < w { curr_orig.resize(w, 0); }

        prev_row[..w].copy_from_slice(&y_plane[..w]);

        for y in 1..h - 1 {
            let row_off   = y * y_stride;
            let below_off = (y + 1) * y_stride;

            // Копия текущей строки ДО модификации
            curr_orig[..w].copy_from_slice(&y_plane[row_off..row_off + w]);

            for x in 1..w - 1 {
                let c = curr_orig[x] as i16;
                let blur = (prev_row[x] as i16
                          + y_plane[below_off + x] as i16
                          + curr_orig[x - 1] as i16
                          + curr_orig[x + 1] as i16) >> 2;
                let sharp = c + (c - blur) * strength / 10;
                y_plane[row_off + x] = sharp.clamp(0, 255) as u8;
            }
            prev_row[..w].copy_from_slice(&curr_orig[..w]);
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
        if bitrate == self.bitrate {
            return Ok(());
        }

        let ratio      = bitrate as f64 / self.bitrate as f64;
        let pct_change = ((ratio - 1.0) * 100.0).abs();

        info!(
            "[ABR] {} → {} kbps ({:+.0}%)",
            self.bitrate / 1000, bitrate / 1000,
            (ratio - 1.0) * 100.0
        );

        if pct_change <= 20.0 {
            self.bitrate = bitrate;
            self.force_keyframe = true;
            info!("[ABR] soft: IDR only, без пересоздания");
            return Ok(());
        }

        if self.try_apply_bitrate_inplace(bitrate) {
            self.bitrate = bitrate;
            self.force_keyframe = true;
            info!("[ABR] in-place: AVCodecContext обновлён + IDR");
            return Ok(());
        }

        self.force_keyframe = true;

        let new = Self::try_create(
            &self.profile, self.width, self.height, self.fps, bitrate,
        )
        .with_context(|| format!("set_bitrate rebuild: {} kbps", bitrate / 1000))?;

        self.encoder     = new.encoder;
        self.scaler      = new.scaler;
        self.bitrate     = bitrate;
        self.frame_index = 0;
        self.src_width   = 0;
        self.src_height  = 0;
        self.force_keyframe = true;
        // Также сбрасываем кэшированный BGRA buffer — после пересоздания
        // swscale первое сравнение size пройдёт заново.
        self.bgra_frame = None;
        self.bgra_frame_dims = (0, 0);
        info!("[ABR] rebuild: новый энкодер {} kbps", bitrate / 1000);
        Ok(())
    }

    fn try_apply_bitrate_inplace(&mut self, bitrate: u32) -> bool {
        match self.profile.codec_name.as_str() {
            "libx264" | "h264_qsv" => return false,
            _ => {}
        }
        unsafe {
            let ctx = self.encoder.as_mut_ptr();
            if ctx.is_null() {
                return false;
            }
            (*ctx).bit_rate       = bitrate as i64;
            (*ctx).rc_max_rate    = bitrate as i64;
            (*ctx).rc_buffer_size = bitrate as i32;
        }
        true
    }

    pub fn encode_yuv(
        &mut self,
        mut yuv_frame: AvFrame,
        timestamp_ns: u64,
    ) -> Result<Vec<EncodedPacket>> {
        if self.sharpen_strength > 0 {
            Self::sharpen_y_plane_inplace(
                &mut yuv_frame,
                self.width,
                self.height,
                self.sharpen_strength,
                &mut self.sharpen_prev_row,
                &mut self.sharpen_curr_row,
            );
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
                data:         pkt.data().unwrap_or(&[]).to_vec(),
                pts:          pkt.pts().unwrap_or(0),
                dts:          pkt.dts().unwrap_or(0),
                is_key:       pkt.is_key(),
                timestamp_ns,
            });
        }
        Ok(packets)
    }

    pub fn codec_name(&self) -> &str  { &self.profile.display_name }
    pub fn is_hardware(&self) -> bool { self.profile.is_hardware }
}

// ─── Frame Dedup ─────────────────────────────────────────────────────────────
/// Сравнивает два BGRA-кадра по сэмплированным пикселям.
pub fn frame_changed(prev: &[u8], curr: &[u8], width: u32, height: u32, stride: u32) -> bool {
    let total = (width * height) as usize;
    let step: usize = 16;
    let thresh: u8 = 8;
    let limit = total / step / 300;

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

// ─── LQ Encoder (Simulcast) ──────────────────────────────────────────────────

pub struct LqEncoder {
    inner:           HwEncoder,
    combined_scaler: scaling::Context,
    src_width:       u32,
    src_height:      u32,
    lq_width:        u32,
    lq_height:       u32,
    // FIX #34: переиспользуемый BGRA AvFrame
    bgra_frame:      Option<AvFrame>,
    bgra_frame_dims: (u32, u32),
}

impl LqEncoder {
    pub fn new(
        src_w: u32, src_h: u32,
        lq_w:  u32, lq_h:  u32,
        fps:   u32, br:    u32,
    ) -> Result<Self> {
        let inner = HwEncoder::new(lq_w, lq_h, fps, br)?;

        let combined_scaler = scaling::Context::get(
            Pixel::BGRA,    src_w, src_h,
            Pixel::YUV420P, lq_w,  lq_h,
            scaling::Flags::AREA,
        )
        .context("LQ combined_scaler BGRA→YUV (AREA)")?;

        info!(
            "LqEncoder: {}×{} → {}×{} @ {} fps, {} kbps [single-pass AREA]",
            src_w, src_h, lq_w, lq_h, fps, br / 1000
        );

        Ok(Self {
            inner,
            combined_scaler,
            src_width:  src_w,
            src_height: src_h,
            lq_width:   lq_w,
            lq_height:  lq_h,
            bgra_frame: None,
            bgra_frame_dims: (0, 0),
        })
    }

    pub fn encode(&mut self, frame: &CapturedFrame) -> Result<Vec<EncodedPacket>> {
        if frame.width != self.src_width || frame.height != self.src_height {
            self.combined_scaler = scaling::Context::get(
                Pixel::BGRA,    frame.width,  frame.height,
                Pixel::YUV420P, self.lq_width, self.lq_height,
                scaling::Flags::AREA,
            )
            .context("LQ scaler resize")?;

            self.src_width  = frame.width;
            self.src_height = frame.height;
            self.inner.request_keyframe();
            // сбрасываем кэшированный BGRA buffer
            self.bgra_frame = None;
            self.bgra_frame_dims = (0, 0);

            info!(
                "LQ scaler пересоздан: {}×{} → {}×{}",
                frame.width, frame.height, self.lq_width, self.lq_height
            );
        }

        // FIX #34: переиспользуем BGRA AvFrame
        if self.bgra_frame.is_none() || self.bgra_frame_dims != (frame.width, frame.height) {
            self.bgra_frame = Some(AvFrame::new(Pixel::BGRA, frame.width, frame.height));
            self.bgra_frame_dims = (frame.width, frame.height);
        }

        let bgra_frame = self.bgra_frame.as_mut().unwrap();
        {
            let stride = bgra_frame.stride(0);
            let plane  = bgra_frame.data_mut(0);
            for row in 0..frame.height as usize {
                let ss = row * frame.stride as usize;
                let se = ss + (frame.width * 4) as usize;
                let ds = row * stride;
                let de = ds + (frame.width * 4) as usize;
                if se <= frame.data.len() && de <= plane.len() {
                    plane[ds..de].copy_from_slice(&frame.data[ss..se]);
                }
            }
        }

        let mut yuv_frame = AvFrame::empty();
        self.combined_scaler.run(bgra_frame, &mut yuv_frame)?;

        self.inner.encode_yuv(yuv_frame, frame.timestamp_ns)
    }

    pub fn request_keyframe(&mut self) { self.inner.request_keyframe(); }
    pub fn flush(&mut self) -> Result<Vec<EncodedPacket>> { self.inner.flush() }
}
