//! Explicit local coordinator/provider review: no platform writer or SIP bridge.
//! Input is private config plus 16-kHz mono signed PCM16. Output is private traces.
use serde_json::json;
use sg_voice_prototype::{
    playout::{AudioSink, Capture, Playout},
    rvc::{
        coordinator::{self, Output},
        Config, Trace,
    },
};
use std::{path::Path, sync::Arc, time::Duration};
struct Sink(Arc<tokio::sync::Notify>);
impl AudioSink for Sink {
    fn capture(&self, _pcm: Vec<i16>) -> Capture<'_> {
        self.0.notify_one();
        Box::pin(async {
            tokio::time::sleep(Duration::from_millis(10)).await;
            Ok(())
        })
    }
    fn clear(&self) {}
}
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() != 5
        || !matches!(
            args[4].as_str(),
            "baseline" | "stream" | "stream-interrupt" | "stream-human"
        )
    {
        return Err(
            "usage: rvc-transfer-review PRIVATE_CONFIG PCM16 PRIVATE_OUTPUT baseline|stream|stream-interrupt|stream-human".into(),
        );
    }
    let mut c: Config = serde_json::from_slice(&std::fs::read(&args[1])?)?;
    c.transfer_streaming_replies = args[4] != "baseline";
    c.persist_conversation = false;
    c.base_system_prompt
        .push_str(&sg_voice_prototype::rvc::languages::rules(&c.tts_language));
    let context = c.agent_context.clone();
    sg_voice_prototype::rvc::languages::apply(&mut c, &context, &json!({}))?;
    c.validate()?;
    let bytes = std::fs::read(&args[2])?;
    if bytes.is_empty() || bytes.len() % 2 != 0 || bytes.len() > 16000 * 2 * 60 {
        return Err("expected at most 60 seconds of PCM16".into());
    }
    let pcm: Vec<_> = bytes
        .as_chunks::<2>()
        .0
        .iter()
        .map(|b| i16::from_le_bytes(*b))
        .collect();
    std::fs::create_dir_all(&args[3])?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&args[3], std::fs::Permissions::from_mode(0o700))?;
    }
    let trace = Trace::synchronous(Path::new(&args[3]).join("trace.jsonl"))?;
    let first_audio = Arc::new(tokio::sync::Notify::new());
    let playout = Playout::spawn(Arc::new(Sink(first_audio.clone())), 24000, 1, 24000);
    let (session, mut out, worker) = coordinator::start(Arc::new(c), playout, trace.clone(), true);
    let interrupt = if args[4] == "stream-interrupt" {
        let input = session.input.clone();
        let pcm = pcm.clone();
        let trace = trace.clone();
        Some(tokio::spawn(async move {
            first_audio.notified().await;
            trace.emit("review_caller_interrupt_started", 0, 0, json!({}));
            for block in pcm
                .chunks(320)
                .map(|b| b.to_vec())
                .chain(std::iter::repeat_n(vec![0; 320], 50))
            {
                if input.send(coordinator::Input::Pcm(block)).await.is_err() {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        }))
    } else {
        None
    };
    let input = session.input.clone();
    let feed = tokio::spawn(async move {
        tokio::time::sleep(Duration::from_secs(2)).await;
        for block in pcm
            .chunks(320)
            .map(|b| b.to_vec())
            .chain(std::iter::repeat_n(vec![0; 320], 50))
        {
            if input.send(coordinator::Input::Pcm(block)).await.is_err() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    });
    let result = tokio::time::timeout(Duration::from_secs(75), async {
        let mut answer = String::new();
        while let Some(event) = out.recv().await {
            match event {
                Output::Answer(text) => answer = text,
                Output::Complete { turn }
                    if turn >= if args[4] == "stream-interrupt" { 2 } else { 1 } =>
                {
                    return Ok::<_, String>(answer)
                }
                Output::Error(_) => {
                    return Err("coordinator provider error; inspect private trace".into())
                }
                Output::Transfer { reply, cleanup, .. } if args[4] == "stream-human" => {
                    let _ = cleanup.send(true);
                    let _ = reply.send(Err(json!({"status":"failed","cleanup_confirmed":true})));
                    return Ok(answer);
                }
                Output::Transfer { .. } => {
                    return Err(
                        "review blocked an unexpected transfer; no SIP client exists".into(),
                    )
                }
                _ => {}
            }
        }
        Err("coordinator stopped".into())
    })
    .await;
    feed.abort();
    if let Some(task) = interrupt {
        task.abort();
    }
    session.close().await;
    worker.await?;
    let answer = result??;
    std::fs::write(Path::new(&args[3]).join("answer.txt"), &answer)?;
    let events = trace.events();
    if args[4] == "stream-interrupt"
        && !events
            .iter()
            .any(|e| e.event == "attempt_cancel" && e.data["reason"] == "user_barge_in")
    {
        return Err("interruption was not observed".into());
    }
    if args[4] == "stream-human" && !events.iter().any(|e| e.event == "human_transfer_requested") {
        return Err("human bridge intent was not observed".into());
    }
    let summary = json!({"mode":args[4],"answer_chars":answer.chars().count(),"usage":session.usage.snapshot(true),"timings":events.iter().filter(|e|e.turn>0&&matches!(e.event.as_str(),"user_speech_end"|"vad_commit"|"asr_final_response"|"final_text_received"|"audio_first_enqueued"|"llm_first_token"|"llm_request_start"|"llm_request_complete"|"transfer_route_selected"|"tts_first_pcm"|"speculation_start"|"speculation_validate")).collect::<Vec<_>>()});
    std::fs::write(
        Path::new(&args[3]).join("summary.json"),
        serde_json::to_vec_pretty(&summary)?,
    )?;
    println!(
        "{}",
        json!({"mode":args[4],"answer_chars":answer.chars().count(),"ok":true})
    );
    Ok(())
}
