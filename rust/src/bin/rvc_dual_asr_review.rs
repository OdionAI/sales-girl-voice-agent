//! Private local native ASR/LLM/TTS review; no platform writes or SIP client.
use serde_json::json;
use sg_voice_prototype::{
    playout::{AudioSink, Capture, Playout},
    rvc::{
        coordinator::{self, Input, Output},
        languages, Config, Trace,
    },
};
use std::{collections::BTreeSet, path::Path, sync::Arc, time::Duration};
struct Sink;
impl AudioSink for Sink {
    fn capture(&self, _: Vec<i16>) -> Capture<'_> {
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
    if args.len() < 4 {
        return Err(
            "usage: rvc-dual-asr-review PRIVATE_CONFIG PRIVATE_OUTPUT PCM16 [PCM16 ...]".into(),
        );
    }
    let mut c: Config = serde_json::from_slice(&std::fs::read(&args[1])?)?;
    c.persist_conversation = false;
    c.opening_greeting_enabled = false;
    c.balogun_number_capture_enabled = true;
    let ctx = c.agent_context.clone();
    languages::apply(&mut c, &ctx, &json!({}))?;
    c.base_system_prompt
        .push_str(&languages::rules(&c.tts_language));
    c.validate()?;
    std::fs::create_dir_all(&args[2])?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&args[2], std::fs::Permissions::from_mode(0o700))?;
    }
    let dir = Path::new(&args[2]);
    let trace = Trace::synchronous(dir.join("trace.jsonl"))?;
    let (session, mut out, worker) = coordinator::start(
        Arc::new(c),
        Playout::spawn(Arc::new(Sink), 24000, 1, 24000),
        trace.clone(),
        true,
    );
    tokio::time::sleep(Duration::from_secs(2)).await;
    let mut completed = BTreeSet::new();
    let mut answers = Vec::new();
    let mut error = None;
    for file in &args[3..] {
        let data = std::fs::read(file)?;
        if data.len() % 2 != 0 || data.len() > 16000 * 2 * 60 {
            return Err("expected bounded 16 kHz PCM16".into());
        }
        let pcm: Vec<_> = data
            .as_chunks::<2>()
            .0
            .iter()
            .map(|b| i16::from_le_bytes(*b))
            .collect();
        let input = session.input.clone();
        let feed = tokio::spawn(async move {
            for b in pcm
                .chunks(320)
                .map(|b| b.to_vec())
                .chain(std::iter::repeat_n(vec![0; 320], 50))
            {
                if input.send(Input::Pcm(b)).await.is_err() {
                    return;
                }
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        });
        let run=tokio::time::timeout(Duration::from_secs(75),async {
   loop {
    if feed.is_finished() {
     let turn=trace.events().iter().rev().find(|e|e.event=="user_speech_start").map(|e|e.turn);
     if turn.is_some_and(|t|completed.contains(&t)){break Ok::<_,String>(());}
    }
    tokio::select! {
     e=out.recv()=>match e {
      Some(Output::Complete{turn})=>{completed.insert(turn);},
      Some(Output::Answer(text))=>answers.push(text),
      Some(Output::Error(e))=>break Err(e),
      Some(Output::Transfer{..})=>break Err("unexpected transfer blocked: this review has no SIP bridge".into()),
      None=>break Err("worker stopped".into()),_=>{}
     },
     _=tokio::time::sleep(Duration::from_millis(50))=>{}
    }
   }
  }).await;
        feed.abort();
        match run {
            Ok(Ok(())) => {}
            Ok(Err(e)) => {
                error = Some(e);
                break;
            }
            Err(_) => {
                error = Some("review timed out".into());
                break;
            }
        }
    }
    session.close().await;
    worker.await?;
    std::fs::write(
        dir.join("answers.json"),
        serde_json::to_vec_pretty(&answers)?,
    )?;
    std::fs::write(
        dir.join("secondary-private.json"),
        serde_json::to_vec_pretty(&session.secondary_review_reference())?,
    )?;
    let events = trace.events();
    let count = |name: &str| events.iter().filter(|e| e.event == name).count();
    let summary = json!({"utterances":count("user_speech_end"),"qwen_submitted":count("parallel_qwen_asr_submitted"),"qwen_complete":count("parallel_qwen_asr_complete"),"qwen_failed":count("parallel_qwen_asr_failed"),"qwen_attached":count("parallel_qwen_asr_attached_to_llm"),"qwen_numeric_fallback":count("parallel_qwen_asr_numeric_fallback"),"answers":answers.len(),"usage":session.usage.snapshot(true),"ok":error.is_none()});
    std::fs::write(
        dir.join("summary.json"),
        serde_json::to_vec_pretty(&summary)?,
    )?;
    println!("{summary}");
    if let Some(e) = error {
        std::fs::write(dir.join("error.txt"), e)?;
        return Err("provider review failed; inspect private output".into());
    }
    Ok(())
}
