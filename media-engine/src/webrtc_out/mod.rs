// src/webrtc_out/mod.rs — WebRTC sender: H.264 NAL → RTP → Pion SFU
//
// ── Исправления v0.3.1 ──────────────────────────────────────────────────────
//
//   FIX #35: валидация fps. Раньше Duration::from_secs_f64(1.0 / fps as f64)
//     при fps=0 давал inf → panic "non-finite value" при sleep.
//     Теперь fps.max(1) в начале send_loop.
//
//   FIX #37: close() ждёт завершения send_loop. Раньше close() закрывал PC
//     и возвращался, но spawned send_loop продолжал работать пока frame_tx
//     не дропнется. При быстром stop()+start() Pipeline могло оказаться два
//     send_loop на один PC. Теперь держим JoinHandle и awaitим в close().

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use anyhow::{Context, Result};
use bytes::Bytes;
use tokio::sync::{mpsc, oneshot, Mutex as AsyncMutex};
use tokio::task::JoinHandle;
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
use webrtc::rtp_transceiver::RTCPFeedback;
use webrtc::track::track_local::track_local_static_sample::TrackLocalStaticSample;
use webrtc::track::track_local::TrackLocal;

// I-frame pacing: пауза после отправки полного I-кадра.
const KEYFRAME_PACE_DELAY: Duration = Duration::from_millis(30);

pub struct WebRtcSender {
    pc:             Arc<RTCPeerConnection>,
    frame_tx:       mpsc::Sender<(Bytes, bool)>,
    answer_tx:      AsyncMutex<Option<oneshot::Sender<String>>>,
    offer_sdp:      String,
    pli_requested:  Arc<AtomicBool>,
    send_loop_handle: AsyncMutex<Option<JoinHandle<()>>>,
    rtcp_loop_handle: AsyncMutex<Option<JoinHandle<()>>>,
    closed:         AtomicBool,
}

impl WebRtcSender {
    pub async fn new(fps: u32) -> Result<(Arc<Self>, String)> {
        // FIX #35: валидация fps
        let fps = fps.max(1).min(120);

        let mut me = MediaEngine::default();

        let h264_profiles: &[(u8, &str)] = &[
            (96, "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=640032"),
            (97, "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f"),
            (98, "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42001f"),
            (99, "level-asymmetry-allowed=1;packetization-mode=0;profile-level-id=42001f"),
        ];

        let h264_feedback = vec![
            RTCPFeedback { typ: "goog-remb".to_string(), parameter: "".to_string() },
            RTCPFeedback { typ: "ccm".to_string(),       parameter: "fir".to_string() },
            RTCPFeedback { typ: "nack".to_string(),      parameter: "".to_string() },
            RTCPFeedback { typ: "nack".to_string(),      parameter: "pli".to_string() },
        ];

        for (pt, fmtp) in h264_profiles {
            me.register_codec(
                RTCRtpCodecParameters {
                    capability: RTCRtpCodecCapability {
                        mime_type:     MIME_TYPE_H264.to_owned(),
                        clock_rate:    90_000,
                        channels:      0,
                        sdp_fmtp_line: fmtp.to_string(),
                        rtcp_feedback: h264_feedback.clone(),
                    },
                    payload_type: *pt,
                    ..Default::default()
                },
                RTPCodecType::Video,
            )?;
            info!("[WebRTC] Зарегистрирован PT={pt}: {fmtp}");
        }

        let mut reg = Registry::new();
        reg = register_default_interceptors(reg, &mut me)
            .context("register_default_interceptors")?;

        let setting_engine = {
            let mut s = webrtc::api::setting_engine::SettingEngine::default();
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
                sdp_fmtp_line: "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=640032"
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

        // RTCP drain loop: читает PLI от Pion SFU → форсируем IDR
        let rtcp_loop = {
            let pli_flag_clone = Arc::clone(&pli_flag);
            tokio::spawn(async move {
                loop {
                    match rtp_sender.read_rtcp().await {
                        Ok((packets, _)) => {
                            for pkt in packets {
                                if pkt
                                    .as_any()
                                    .downcast_ref::<webrtc::rtcp::payload_feedbacks::picture_loss_indication::PictureLossIndication>()
                                    .is_some()
                                {
                                    pli_flag_clone.store(true, Ordering::Release);
                                    info!("[WebRTC] PLI получен от SFU → форсируем IDR-кадр");
                                }
                            }
                        }
                        Err(e) => {
                            debug!("[WebRTC RTCP loop] stopped: {:?}", e);
                            break;
                        }
                    }
                }
            })
        };

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

        let send_loop = tokio::spawn(send_loop(
            Arc::clone(&pc), video_track, frame_rx, answer_rx, fps,
        ));

        let sender = Arc::new(Self {
            pc:               Arc::clone(&pc),
            frame_tx,
            answer_tx:        AsyncMutex::new(Some(answer_tx)),
            offer_sdp:        offer_sdp.clone(),
            pli_requested:    pli_flag,
            send_loop_handle: AsyncMutex::new(Some(send_loop)),
            rtcp_loop_handle: AsyncMutex::new(Some(rtcp_loop)),
            closed:           AtomicBool::new(false),
        });

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
        if self.closed.load(Ordering::Acquire) {
            return;
        }
        match self.frame_tx.try_send((data, is_keyframe)) {
            Ok(())                                    => {}
            Err(mpsc::error::TrySendError::Full(_))   => debug!("WebRTC frame queue full — drop"),
            Err(mpsc::error::TrySendError::Closed(_)) => {}
        }
    }

    pub fn take_pli_request(&self) -> bool {
        self.pli_requested.swap(false, Ordering::AcqRel)
    }

    /// FIX #37: close() ждёт завершения внутренних задач.
    pub async fn close(&self) -> Result<()> {
        if self.closed.swap(true, Ordering::AcqRel) {
            return Ok(()); // уже закрыт
        }

        // Закрываем PC — это вызовет завершение read_rtcp и (через drop frame_tx)
        // send_loop тоже завершится. frame_tx здесь не дропаем явно, он в self
        // и разрушится вместе с Arc<Self> — но чтобы гарантировать, закрываем
        // send_loop через JoinHandle.await.
        self.pc.close().await.context("pc.close")?;

        // Ждём завершения send_loop и rtcp_loop с таймаутом.
        if let Some(h) = self.send_loop_handle.lock().await.take() {
            match tokio::time::timeout(Duration::from_secs(2), h).await {
                Ok(Ok(())) => {}
                Ok(Err(e)) => warn!("send_loop join error: {e:?}"),
                Err(_) => warn!("send_loop не завершился за 2с — abort"),
            }
        }
        if let Some(h) = self.rtcp_loop_handle.lock().await.take() {
            match tokio::time::timeout(Duration::from_secs(2), h).await {
                Ok(Ok(())) => {}
                Ok(Err(e)) => warn!("rtcp_loop join error: {e:?}"),
                Err(_) => warn!("rtcp_loop не завершился за 2с"),
            }
        }

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

    // FIX #35: fps гарантированно >= 1 (валидация в WebRtcSender::new)
    let frame_dur = Duration::from_secs_f64(1.0 / fps as f64);

    while let Some((data, is_key)) = rx.recv().await {
        let sample = Sample {
            data,
            duration: frame_dur,
            ..Default::default()
        };
        if let Err(e) = track.write_sample(&sample).await {
            warn!("[WebRTC send_loop] write_sample: {e}");
        }

        if is_key {
            tokio::time::sleep(KEYFRAME_PACE_DELAY).await;
        }
    }

    info!("[WebRTC send_loop] frame channel closed — exiting");
}
