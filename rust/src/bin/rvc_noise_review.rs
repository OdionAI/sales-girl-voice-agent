//! Offline filter review only: 16 kHz mono PCM16 input/output, no providers.
use sg_voice_prototype::rvc::{input_noise::InputNoiseFilter, Config, Trace};
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() != 4 || !matches!(args[3].as_str(), "webrtc" | "rnnoise") {
        return Err("usage: rvc-noise-review INPUT_S16LE OUTPUT_S16LE webrtc|rnnoise".into());
    }
    let bytes = std::fs::read(&args[1])?;
    if bytes.is_empty() || !bytes.len().is_multiple_of(2) || bytes.len() > 16000 * 2 * 300 {
        return Err("expected at most five minutes of 16 kHz PCM16".into());
    }
    let pcm: Vec<i16> = bytes
        .as_chunks::<2>()
        .0
        .iter()
        .map(|b| i16::from_le_bytes([b[0], b[1]]))
        .collect();
    let mut config: Config =
        serde_json::from_str(include_str!("../../fixtures/reference-config.json"))?;
    config.input_noise_suppression = true;
    config.input_noise_agent_ids = vec!["filter-review".into()];
    config.agent_context.agent_id = "filter-review".into();
    config.input_noise_implementation = args[3].clone();
    let trace = Trace::default();
    let mut filter = InputNoiseFilter::new(&config, trace.clone());
    let started = std::time::Instant::now();
    let mut result = vec![];
    for block in pcm.chunks(1600) {
        result.extend(filter.process(block.to_vec()));
    }
    result.extend(filter.finish());
    std::fs::write(
        &args[2],
        result
            .iter()
            .flat_map(|s| s.to_le_bytes())
            .collect::<Vec<_>>(),
    )?;
    println!(
        "{}",
        serde_json::json!({"implementation":args[3],"input_samples":pcm.len(),"output_samples":result.len(),"wall_ms":started.elapsed().as_secs_f64()*1000.0,"trace":trace.events()})
    );
    Ok(())
}
