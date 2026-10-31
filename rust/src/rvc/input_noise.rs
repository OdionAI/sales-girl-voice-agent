//! Operator-selected native WebRTC or RNNoise suppression before VAD/ASR.
//! Resampling uses LiveKit; echo cancellation and automatic gain stay disabled.
//!
//! The RNNoise path links Xiph RNNoise v0.2 (the same model the Python worker's
//! `pyrnnoise` uses) via the vendored `third_party/rnnoise` C library built in
//! `build.rs`. Earlier revisions used the pure-Rust `nnnoiseless` 0.5.2 port of
//! the *old* v0.1 model, which barely attenuated steady background noise and even
//! scored pure tones as speech, so ambient noise still tripped the RMS speech
//! gate and interrupted the agent.
use super::{Config, Trace};
use livekit::webrtc::native::{apm::AudioProcessingModule, audio_resampler::AudioResampler};
use serde_json::json;
use std::ffi::c_void;

/// RNNoise processes fixed 10 ms frames at 48 kHz (`rnnoise_get_frame_size()`).
const RNNOISE_RATE: u32 = 48_000;
const RNNOISE_FRAME: usize = 480;
/// Caller audio arrives at 16 kHz mono; 10 ms is 160 samples.
const WORK_FRAME: usize = 160;

extern "C" {
    // Opaque RNNoise handles; the model argument may be null for the default.
    fn rnnoise_create(model: *mut c_void) -> *mut c_void;
    fn rnnoise_destroy(state: *mut c_void);
    fn rnnoise_process_frame(state: *mut c_void, out: *mut f32, input: *const f32) -> f32;
    fn rnnoise_get_frame_size() -> i32;
}

enum Backend {
    Off,
    Webrtc(AudioProcessingModule),
    Rnnoise(NativeRnnoise),
}
struct NativeRnnoise {
    state: *mut c_void,
    up: AudioResampler,
    down: AudioResampler,
}
// The denoise state is owned by a single call actor and only touched from that
// task, but the actor may move between worker threads.
unsafe impl Send for NativeRnnoise {}
impl NativeRnnoise {
    fn new() -> Option<Self> {
        if unsafe { rnnoise_get_frame_size() } as usize != RNNOISE_FRAME {
            return None;
        }
        let state = unsafe { rnnoise_create(std::ptr::null_mut()) };
        if state.is_null() {
            return None;
        }
        Some(Self {
            state,
            up: Default::default(),
            down: Default::default(),
        })
    }
    fn process(&mut self, frame: &mut [i16]) -> Result<f32, ()> {
        // Both RNNoise and the official SDK resampler require complete 10 ms
        // frames. Preserve PCM16 amplitude scale, not normalized [-1,1] values.
        if frame.len() != WORK_FRAME {
            return Err(());
        }
        let up = self
            .up
            .remix_and_resample(frame, WORK_FRAME as u32, 1, 16_000, 1, RNNOISE_RATE);
        if up.len() != RNNOISE_FRAME {
            return Err(());
        }
        let mut input = [0.0f32; RNNOISE_FRAME];
        for (target, sample) in input.iter_mut().zip(up) {
            *target = f32::from(*sample);
        }
        let mut output = [0.0f32; RNNOISE_FRAME];
        let probability =
            unsafe { rnnoise_process_frame(self.state, output.as_mut_ptr(), input.as_ptr()) };
        if !probability.is_finite() || output.iter().any(|sample| !sample.is_finite()) {
            return Err(());
        }
        let mut pcm = [0i16; RNNOISE_FRAME];
        for (target, sample) in pcm.iter_mut().zip(output) {
            *target = sample.round().clamp(-32768.0, 32767.0) as i16;
        }
        let down =
            self.down
                .remix_and_resample(&pcm, RNNOISE_FRAME as u32, 1, RNNOISE_RATE, 1, 16_000);
        if down.len() != WORK_FRAME {
            return Err(());
        }
        frame.copy_from_slice(down);
        Ok(probability.clamp(0.0, 1.0))
    }
}
impl Drop for NativeRnnoise {
    fn drop(&mut self) {
        unsafe { rnnoise_destroy(self.state) };
    }
}
pub struct InputNoiseFilter {
    backend: Backend,
    speech_prob_total: f64,
    speech_prob_frames: usize,
    pending: Vec<i16>,
    trace: Trace,
    samples: usize,
    frames: usize,
    input_energy: f64,
    output_energy: f64,
    processing_us: u128,
}
impl InputNoiseFilter {
    pub fn new(config: &Config, trace: Trace) -> Self {
        let selected = config.input_noise_suppression
            && config
                .input_noise_agent_ids
                .iter()
                .map(|id| id.trim())
                .any(|id| !id.is_empty() && (id == "*" || id == config.agent_context.agent_id));
        let rnnoise = config
            .input_noise_implementation
            .trim()
            .eq_ignore_ascii_case("rnnoise");
        let backend = if !selected {
            Backend::Off
        } else if rnnoise {
            match NativeRnnoise::new() {
                Some(native) => Backend::Rnnoise(native),
                None => {
                    trace.emit(
                        "input_noise_filter_error",
                        0,
                        0,
                        json!({"stage":"init","implementation":"rnnoise","fallback":"unfiltered"}),
                    );
                    Backend::Off
                }
            }
        } else {
            Backend::Webrtc(AudioProcessingModule::new(false, false, false, true))
        };
        trace.emit("input_noise_filter_ready",0,0,json!({"enabled":selected,"implementation":if !selected{"off"}else if rnnoise{"rnnoise"}else{"webrtc"},"backend":if !selected{"none"}else if rnnoise{"xiph-rnnoise-0.2"}else{"official_livekit_apm"},"sample_rate":16000,"frame_ms":10,"echo_cancellation":false,"auto_gain_control":false}));
        Self {
            backend,
            speech_prob_total: 0.0,
            speech_prob_frames: 0,
            pending: vec![],
            trace,
            samples: 0,
            frames: 0,
            input_energy: 0.0,
            output_energy: 0.0,
            processing_us: 0,
        }
    }
    pub fn process(&mut self, pcm: Vec<i16>) -> Vec<i16> {
        if matches!(self.backend, Backend::Off) {
            return pcm;
        }
        self.pending.extend(pcm);
        let count = self.pending.len() / 160 * 160;
        if count == 0 {
            return vec![];
        }
        let tail = self.pending.split_off(count);
        let mut output = std::mem::replace(&mut self.pending, tail);
        let raw = output.clone();
        let start = std::time::Instant::now();
        let mut probabilities = Vec::new();
        let (implementation, result) = match &mut self.backend {
            Backend::Off => ("off", Ok(())),
            Backend::Webrtc(apm) => (
                "webrtc",
                apm.process_stream(&mut output, 16000, 1).map_err(|_| ()),
            ),
            Backend::Rnnoise(rnn) => (
                "rnnoise",
                output
                    .as_chunks_mut::<160>()
                    .0
                    .iter_mut()
                    .try_for_each(|frame| {
                        rnn.process(frame)
                            .map(|probability| probabilities.push(probability))
                    }),
            ),
        };
        if result.is_err() {
            self.backend = Backend::Off;
            self.trace.emit(
                "input_noise_filter_error",
                0,
                0,
                json!({"stage":"process","implementation":implementation,"fallback":"unfiltered"}),
            );
            let mut original = raw;
            original.append(&mut self.pending);
            return original;
        }
        self.processing_us += start.elapsed().as_micros();
        self.speech_prob_total += probabilities
            .iter()
            .map(|value| f64::from(*value))
            .sum::<f64>();
        self.speech_prob_frames += probabilities.len();
        self.samples += raw.len();
        self.frames += raw.len() / 160;
        self.input_energy += raw.iter().map(|s| f64::from(*s).powi(2)).sum::<f64>();
        self.output_energy += output.iter().map(|s| f64::from(*s).powi(2)).sum::<f64>();
        if self.frames >= 500 {
            self.report();
        }
        output
    }
    fn report(&mut self) {
        if self.samples == 0 {
            return;
        }
        self.trace.emit("input_noise_filter_metrics",0,0,json!({"frames":self.frames,"samples":self.samples,"input_rms":(self.input_energy/self.samples as f64).sqrt(),"output_rms":(self.output_energy/self.samples as f64).sqrt(),"processing_ms":self.processing_us as f64/1000.0,"speech_prob":if self.speech_prob_frames>0{Some(self.speech_prob_total/self.speech_prob_frames as f64)}else{None}}));
        self.samples = 0;
        self.frames = 0;
        self.input_energy = 0.0;
        self.output_energy = 0.0;
        self.processing_us = 0;
        self.speech_prob_total = 0.0;
        self.speech_prob_frames = 0;
    }
    pub fn finish(&mut self) -> Vec<i16> {
        let count = self.pending.len();
        let output = if count > 0 {
            let mut output = self.process(vec![0; 160 - count]);
            output.truncate(count);
            output
        } else {
            vec![]
        };
        self.report();
        output
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    fn config() -> Config {
        serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap()
    }
    #[test]
    fn disabled_or_unselected_filter_preserves_exact_audio() {
        let mut c = config();
        c.input_noise_suppression = true;
        c.input_noise_agent_ids = vec!["other-agent".into()];
        let mut filter = InputNoiseFilter::new(&c, Trace::default());
        let pcm = vec![1, 2, -3, 32767, -32768];
        assert_eq!(filter.process(pcm.clone()), pcm);
        assert!(filter.finish().is_empty());
    }
    #[test]
    fn native_noise_selection_matches_independent_python_scope() {
        let fixture: serde_json::Value =
            serde_json::from_str(include_str!("../../fixtures/python-parity-reference.json"))
                .unwrap();
        for case in fixture["noise_selection"].as_array().unwrap() {
            let mut c = config();
            c.input_noise_suppression = case["enabled"].as_bool().unwrap();
            c.input_noise_agent_ids = case["allowed"]
                .as_str()
                .unwrap()
                .split(',')
                .map(str::to_string)
                .collect();
            c.agent_context.agent_id = case["agent"].as_str().unwrap().into();
            c.input_noise_implementation = case["implementation"].as_str().unwrap().into();
            let trace = Trace::default();
            let _filter = InputNoiseFilter::new(&c, trace.clone());
            let event = trace
                .events()
                .into_iter()
                .find(|e| e.event == "input_noise_filter_ready")
                .unwrap();
            assert_eq!(event.data["enabled"], case["expected_enabled"]);
            assert_eq!(
                event.data["implementation"],
                case["expected_implementation"]
            );
        }
    }
    fn noisy_pcm(samples: usize) -> Vec<i16> {
        let mut seed = 17u32;
        (0..samples)
            .map(|i| {
                seed = seed.wrapping_mul(1664525).wrapping_add(1013904223);
                let noise = ((seed >> 16) as i16 as f64) * 0.05;
                let tone = (i as f64 * 300.0 * std::f64::consts::TAU / 16000.0).sin() * 700.0;
                (noise + tone) as i16
            })
            .collect()
    }
    fn rnnoise_config() -> Config {
        let mut c = config();
        c.input_noise_suppression = true;
        c.input_noise_agent_ids = vec!["*".into()];
        c.input_noise_implementation = "rnnoise".into();
        c
    }
    #[test]
    fn native_rnnoise_filters_pcm_preserves_duration_and_reports_real_probability() {
        let c = rnnoise_config();
        let trace = Trace::default();
        let mut filter = InputNoiseFilter::new(&c, trace.clone());
        let input = noisy_pcm(16000 + 79);
        let mut output = filter.process(input.clone());
        assert_eq!(output.len(), 16000);
        output.extend(filter.finish());
        assert_eq!(output.len(), input.len());
        assert_ne!(output, input);
        assert!(filter.finish().is_empty());
        let events = trace.events();
        assert!(events.iter().any(|e| e.event == "input_noise_filter_ready"
            && e.data["enabled"] == true
            && e.data["implementation"] == "rnnoise"));
        let metrics = events
            .iter()
            .find(|e| e.event == "input_noise_filter_metrics")
            .unwrap();
        assert!((0.0..=1.0).contains(&metrics.data["speech_prob"].as_f64().unwrap()));
        assert!(!events.iter().any(|e| e.event == "input_noise_filter_error"));
    }
    #[test]
    fn native_rnnoise_is_partition_invariant_and_state_is_isolated_per_call() {
        let c = rnnoise_config();
        let input = noisy_pcm(32517);
        let mut full = InputNoiseFilter::new(&c, Trace::default());
        let mut expected = full.process(input.clone());
        expected.extend(full.finish());
        for size in [73, 160, 1600, 2401] {
            let mut filter = InputNoiseFilter::new(&c, Trace::default());
            let mut actual = Vec::new();
            for part in input.chunks(size) {
                actual.extend(filter.process(part.to_vec()));
            }
            actual.extend(filter.finish());
            assert_eq!(actual, expected, "packet size {size}");
        }
        let mut fresh = InputNoiseFilter::new(&c, Trace::default());
        let mut other = InputNoiseFilter::new(&c, Trace::default());
        let first = fresh.process(input[..160].to_vec());
        other.process(input.clone());
        let mut fresh_again = InputNoiseFilter::new(&c, Trace::default());
        assert_eq!(first, fresh_again.process(input[..160].to_vec()));
    }
    fn rms_i16(samples: &[i16]) -> f64 {
        if samples.is_empty() {
            return 0.0;
        }
        (samples
            .iter()
            .map(|s| f64::from(*s) * f64::from(*s))
            .sum::<f64>()
            / samples.len() as f64)
            .sqrt()
    }
    fn loud_noise_pcm(samples: usize) -> Vec<i16> {
        // Deterministic, broadband, stationary background noise (no speech).
        let mut seed = 99u32;
        (0..samples)
            .map(|_| {
                seed = seed.wrapping_mul(1664525).wrapping_add(1013904223);
                (((seed >> 16) as i32 % 12001) - 6000) as i16
            })
            .collect()
    }
    #[test]
    fn native_rnnoise_strongly_attenuates_loud_background_noise() {
        // Regression guard: the previous nnnoiseless/v0.1 backend left loud,
        // stationary noise almost untouched (gain ~0.8), which tripped the RMS
        // speech gate and interrupted the agent. RNNoise v0.2 crushes it.
        let c = rnnoise_config();
        let mut filter = InputNoiseFilter::new(&c, Trace::default());
        let input = loud_noise_pcm(16000);
        let in_rms = rms_i16(&input);
        let mut output = filter.process(input.clone());
        output.extend(filter.finish());
        assert_eq!(output.len(), input.len());
        let out_rms = rms_i16(&output);
        assert!(in_rms > 2000.0, "synthetic input should be loud: {in_rms}");
        assert!(
            out_rms < in_rms * 0.5,
            "loud background noise must be strongly suppressed: in_rms={in_rms} out_rms={out_rms} gain={}",
            out_rms / in_rms
        );
    }
    #[test]
    fn unselected_rnnoise_never_changes_or_buffers_audio() {
        let mut c = rnnoise_config();
        c.input_noise_agent_ids = vec!["not-this-call".into()];
        let mut filter = InputNoiseFilter::new(&c, Trace::default());
        let input = noisy_pcm(317);
        assert_eq!(filter.process(input.clone()), input);
        assert!(filter.finish().is_empty());
    }
    #[test]
    fn official_apm_frames_and_flush_preserve_stream_duration() {
        let mut c = config();
        c.input_noise_suppression = true;
        c.input_noise_agent_ids = vec!["*".into()];
        let trace = Trace::default();
        let mut filter = InputNoiseFilter::new(&c, trace.clone());
        assert!(filter.process(vec![0; 75]).is_empty());
        assert_eq!(filter.process(vec![0; 160]).len(), 160);
        assert_eq!(filter.finish().len(), 75);
        assert!(trace
            .events()
            .iter()
            .any(|e| e.event == "input_noise_filter_ready" && e.data["enabled"] == true));
    }
}
