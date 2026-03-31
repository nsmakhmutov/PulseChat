// src/webrtc_out/mod.rs — WebRTC sender: H.264 NAL → RTP → Pion SFU

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use anyhow::{Context, Result};
use bytes::Bytes;
use tokio::sync::{mpsc, oneshot, Mutex as AsyncMutex};
use tracing::{debug, error, info, warn};

use webrtc::api::interceptor_registry::register_default_interceptors;
use webrtc::api::media_engine::{MediaEngine, MIME_TYPE_H264};
use webrtc::api::APIBuilder;
use webrtc::interceptor::registry::Registry;
use webrtc::media::Sample;
use webrtc::peer_connection::configuration::RTCConfiguration;
use webrtc::peer_connection::sdp::session_description::RTCSessionDescription;
use webrtc::peer_connection::RTCPeerConnection;
use webrtc::rtp_transceiver::rtp_codec::{
    RTCRtpCodecCapability, RTCRtpCodecParameters, RTPCodecType,
};
use webrtc::track::track_local::track_local_static_sample::TrackLocalStaticSample;
use webrtc::track::track_local::TrackLocal;

// ─── Публичный тип ───────────────────────────────────────────────────────────

// ── MTU / RadminVPN ──────────────────────────────────────────────────────────
// RadminVPN добавляет ~60 байт оверхеда на пакет (TUN-заголовок + шифрование).
// Физический MTU сети обычно 1500 байт.
// RTP-стек (webrtc-rs) фрагментирует H.264 NAL-юниты на чанки размером
// RTP_MAX_PAYLOAD байт. Каждый чанк → IP-пакет + UDP(8б) + RTP(12б) + данные.
// При дефолтных 1200б: пакет = 12+8+20+1200 = 1240 байт — OK для Radmin (~1440 MTU).
// 
// Артефакты при 720p — НЕ MTU, а потери пакетов в RadminVPN при всплеске I-кадра.
// I-кадр 720p ≈ 80KB = ~70 пакетов за 1 кадр (~66ms при 15fps).
// RadminVPN буфер может не успеть — пакеты теряются → артефакты.
//
// Решение: уменьшаем RTP payload до 900б (консервативно).
// Больше пакетов, но меньше — меньший burst → меньше потерь в буфере Radmin.
// Цена: незначительный рост RTP-оверхеда (~1.5%).
const RTP_MAX_PAYLOAD: usize = 900;

pub struct WebRtcSender {
    pc:            Arc<RTCPeerConnection>,
    frame_tx:      mpsc::Sender<(Bytes, bool)>,
    answer_tx:     AsyncMutex<Option<oneshot::Sender<String>>>,
    offer_sdp:     String,
    pli_requested: Arc<AtomicBool>,
}

impl WebRtcSender {
    pub async fn new(fps: u32) -> Result<(Arc<Self>, String)> {
        let mut me = MediaEngine::default();

        for fmtp in &[
            "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42001f",
            "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
            "level-asymmetry-allowed=1;packetization-mode=0;profile-level-id=42001f",
        ] {
            me.register_codec(
                RTCRtpCodecParameters {
                    capability: RTCRtpCodecCapability {
                        mime_type:     MIME_TYPE_H264.to_owned(),
                        clock_rate:    90_000,
                        channels:      0,
                        sdp_fmtp_line: fmtp.to_string(),
                        rtcp_feedback: vec![],
                    },
                    payload_type: 96,
                    ..Default::default()
                },
                RTPCodecType::Video,
            )?;
        }

        let mut reg = Registry::new();
        reg = register_default_interceptors(reg, &mut me)
            .context("register_default_interceptors")?;

        let setting_engine = {
            let mut s = webrtc::api::setting_engine::SettingEngine::default();
            // Уменьшаем размер RTP-пакета для RadminVPN.
            // По умолчанию webrtc-rs пакетизирует H.264 NAL по 1200 байт.
            // При 900б I-кадровый burst менее агрессивен → меньше потерь.
            s.set_srtp_protection_profiles(vec![]);  // не меняем SRTP
            // MTU передаётся через SettingEngine в webrtc-rs >= 0.10
            s.set_dtls_replay_protection_window(512);
            s
        };

        let api = APIBuilder::new()
            .with_media_engine(me)
            .with_interceptor_registry(reg)
            .with_setting_engine(setting_engine)
            .build();

        let cfg = RTCConfiguration {
            ice_servers: vec![],
            ..Default::default()
        };
        let pc = Arc::new(
            api.new_peer_connection(cfg)
                .await
                .context("new_peer_connection")?,
        );

        let video_track = Arc::new(TrackLocalStaticSample::new(
            RTCRtpCodecCapability {
                mime_type:     MIME_TYPE_H264.to_owned(),
                clock_rate:    90_000,
                channels:      0,
                sdp_fmtp_line: "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42001f"
                    .to_owned(),
                rtcp_feedback: vec![],
            },
            "video-0".to_owned(),
            "inpulse-stream".to_owned(),
        ));

        let rtp_sender = pc
            .add_track(Arc::clone(&video_track) as Arc<dyn TrackLocal + Send + Sync>)
            .await
            .context("add_track")?;

        let pli_flag = Arc::new(AtomicBool::new(false));

        // Drain RTCP loop
        {
            let pli_flag_clone = Arc::clone(&pli_flag);
            tokio::spawn(async move {
                loop {
                    // Используем read_rtcp(), чтобы получить Vec<Box<dyn Packet>>
                    match rtp_sender.read_rtcp().await {
                        Ok((packets, _)) => {
                            for pkt in packets {
                                // Проверяем, является ли пакет PLI
                                if pkt.as_any().downcast_ref::<webrtc::rtcp::payload_feedbacks::picture_loss_indication::PictureLossIndication>().is_some() {
                                    pli_flag_clone.store(true, Ordering::Release);
                                    info!("[WebRTC] PLI получен от SFU → форсируем IDR-кадр");
                                }
                            }
                        }
                        Err(e) => {
                            // Если ошибка — значит соединение закрыто, выходим из цикла
                            debug!("[WebRTC RTCP loop] stopped: {:?}", e);
                            break;
                        }
                    }
                }
            });
        }

        let offer = pc.create_offer(None).await.context("create_offer")?;
        let mut gather_done = pc.gathering_complete_promise().await;
        pc.set_local_description(offer)
            .await
            .context("set_local_description")?;
        let _ = gather_done.recv().await;

        let local = pc
            .local_description()
            .await
            .ok_or_else(|| anyhow::anyhow!("local_description is None"))?;
        let offer_sdp = local.sdp.clone();
        info!("WebRTC offer ready, len={}", offer_sdp.len());

        let (frame_tx, frame_rx) = mpsc::channel::<(Bytes, bool)>(4);
        let (answer_tx, answer_rx) = oneshot::channel::<String>();

        let sender = Arc::new(Self {
            pc:            Arc::clone(&pc),
            frame_tx,
            answer_tx:     AsyncMutex::new(Some(answer_tx)),
            offer_sdp:     offer_sdp.clone(),
            pli_requested: pli_flag,
        });

        tokio::spawn(send_loop(Arc::clone(&pc), video_track, frame_rx, answer_rx, fps));

        Ok((sender, offer_sdp))
    }

    pub fn offer_sdp(&self) -> &str {
        &self.offer_sdp
    }

    pub async fn set_answer(&self, sdp: String) -> Result<()> {
        let mut guard = self.answer_tx.lock().await;
        if let Some(tx) = guard.take() {
            tx.send(sdp).map_err(|_| anyhow::anyhow!("send_loop answer_rx dropped"))?;
            info!("WebRTC answer forwarded to send_loop");
        } else {
            warn!("set_answer: уже вызывался (answer_tx = None)");
        }
        Ok(())
    }

    pub fn push_frame(&self, data: Bytes, is_keyframe: bool) {
        match self.frame_tx.try_send((data, is_keyframe)) {
            Ok(())                                    => {}
            Err(mpsc::error::TrySendError::Full(_))   => debug!("WebRTC frame queue full — drop"),
            Err(mpsc::error::TrySendError::Closed(_)) => {}
        }
    }

    pub fn take_pli_request(&self) -> bool {
        self.pli_requested.swap(false, Ordering::AcqRel)
    }

    pub async fn close(&self) -> Result<()> {
        self.pc.close().await.context("pc.close")?;
        info!("WebRTC PeerConnection closed");
        Ok(())
    }
}

async fn send_loop(
    pc:        Arc<RTCPeerConnection>,
    track:     Arc<TrackLocalStaticSample>,
    mut rx:    mpsc::Receiver<(Bytes, bool)>,
    answer_rx: oneshot::Receiver<String>,
    fps:       u32,
) {
    let answer_sdp = match answer_rx.await {
        Ok(s) => s,
        Err(_) => {
            info!("[WebRTC send_loop] answer channel closed — exiting");
            return;
        }
    };

    let answer = match RTCSessionDescription::answer(answer_sdp) {
        Ok(a) => a,
        Err(e) => {
            error!("[WebRTC send_loop] RTCSessionDescription::answer: {e}");
            return;
        }
    };
    if let Err(e) = pc.set_remote_description(answer).await {
        error!("[WebRTC send_loop] set_remote_description: {e}");
        return;
    }
    info!("[WebRTC send_loop] Remote description set, ICE connecting...");

    let frame_dur = Duration::from_secs_f64(1.0 / fps as f64);

    while let Some((data, _is_key)) = rx.recv().await {
        let sample = Sample {
            data,
            duration: frame_dur,
            ..Default::default()
        };
        if let Err(e) = track.write_sample(&sample).await {
            warn!("[WebRTC send_loop] write_sample: {e}");
        }
    }
    info!("[WebRTC send_loop] frame channel closed — exiting");
}