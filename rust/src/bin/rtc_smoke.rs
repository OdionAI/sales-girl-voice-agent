//! Local-only SDK capability probe; no providers, dispatch or business tools.
use futures_util::StreamExt;
use livekit::webrtc::{
    audio_source::native::NativeAudioSource, audio_stream::native::NativeAudioStream, prelude::*,
};
use livekit::{options::TrackPublishOptions, prelude::*, DataPacket};
use livekit_api::access_token::{AccessToken, VideoGrants};
use std::{borrow::Cow, time::Duration};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let url = "ws://127.0.0.1:17880";
    let name = format!("sg-rust-probe-{}", std::process::id());
    let token = |identity: &str| {
        AccessToken::with_api_key("devkey", "secret")
            .with_identity(identity)
            .with_grants(VideoGrants {
                room_join: true,
                room: name.clone(),
                ..Default::default()
            })
            .to_jwt()
    };
    let (publisher, _publisher_events) =
        Room::connect(url, &token("probe-pub")?, RoomOptions::default()).await?;
    let (subscriber, mut events) =
        Room::connect(url, &token("probe-sub")?, RoomOptions::default()).await?;
    let source = NativeAudioSource::new(AudioSourceOptions::default(), 24000, 1, 100);
    let track =
        LocalAudioTrack::create_audio_track("probe-pcm", RtcAudioSource::Native(source.clone()));
    publisher
        .local_participant()
        .publish_track(LocalTrack::Audio(track), TrackPublishOptions::default())
        .await?;
    let audio = tokio::time::timeout(Duration::from_secs(10), async {
        loop {
            if let Some(RoomEvent::TrackSubscribed {
                track: RemoteTrack::Audio(audio),
                ..
            }) = events.recv().await
            {
                break audio;
            }
        }
    })
    .await?;
    let mut stream = NativeAudioStream::new(audio.rtc_track(), 24000, 1);
    publisher
        .local_participant()
        .publish_data(DataPacket {
            payload: b"probe-event".to_vec(),
            reliable: true,
            topic: Some("sg-prototype".into()),
            ..Default::default()
        })
        .await?;
    tokio::time::timeout(Duration::from_secs(10), async {
        loop {
            if let Some(RoomEvent::DataReceived { payload, .. }) = events.recv().await {
                if payload.as_slice() == b"probe-event" {
                    break;
                }
            }
        }
    })
    .await?;
    let send_source = source.clone();
    let sender = tokio::spawn(async move {
        for frame_index in 0..50 {
            let data: Vec<i16> = (0..240)
                .map(|i| {
                    ((i + frame_index * 240) as f64 * 997.0 * std::f64::consts::TAU / 24000.0)
                        .sin()
                        .mul_add(8000.0, 0.0) as i16
                })
                .collect();
            send_source
                .capture_frame(&AudioFrame {
                    data: Cow::Owned(data),
                    sample_rate: 24000,
                    num_channels: 1,
                    samples_per_channel: 240,
                })
                .await
                .unwrap();
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    });
    let nonzero = tokio::time::timeout(Duration::from_secs(10), async {
        loop {
            if let Some(frame) = stream.next().await {
                if frame.data.iter().any(|s| i32::from(*s).abs() > 100) {
                    break frame.data.len();
                }
            }
        }
    })
    .await?;
    sender.abort();
    let _ = sender.await;
    source.clear_buffer();
    publisher.close().await?;
    subscriber.close().await?;
    println!("{{\"room_connection\":true,\"pcm_received_samples\":{nonzero},\"data_event\":true,\"producer_cancelled\":true,\"source_cleared\":true,\"audible_playout_complete\":null,\"worker_dispatch\":null}}");
    Ok(())
}
