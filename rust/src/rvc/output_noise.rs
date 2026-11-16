//! Denoise synthesized TTS speech before playout.
//!
//! The Qwen3-TTS model re-adds a faint broadband hiss floor (~-51 dBFS) on
//! every synthesis regardless of how clean the clone reference is, so cleaning
//! the reference does not remove it. Running the same Xiph RNNoise denoiser the
//! inbound path uses over the *output* PCM measurably drops that floor
//! (~-51 -> ~-59 dB) with no change to voice level or intelligibility.
//!
//! The worker emits TTS at 24 kHz mono. RNNoise processes fixed 10 ms frames at
//! 48 kHz, so each 10 ms of output (240 samples @24k) is resampled up to 480
//! samples @48k, denoised, then resampled back. Input chunks are buffered to
//! whole 10 ms frames; a sub-frame tail is passed through untouched on `finish`.

use livekit::webrtc::native::audio_resampler::AudioResampler;
use rnnoise2::Denoiser;

const OUT_RATE: u32 = 24_000;
/// 10 ms of 24 kHz audio.
const OUT_FRAME: usize = 240;
const RN_RATE: u32 = 48_000;
/// RNNoise fixed 10 ms frame at 48 kHz.
const RN_FRAME: usize = 480;

pub(crate) struct OutputDenoise {
    denoiser: Denoiser,
    up: AudioResampler,
    down: AudioResampler,
    pending: Vec<i16>,
}

impl OutputDenoise {
    pub(crate) fn new() -> Option<Self> {
        if Denoiser::frame_size() != RN_FRAME {
            return None;
        }
        Some(Self {
            denoiser: Denoiser::new(None)?,
            up: Default::default(),
            down: Default::default(),
            pending: Vec::new(),
        })
    }

    /// Denoise whatever whole 10 ms frames are available in this chunk; the
    /// returned audio preserves duration (same sample count for frame-aligned
    /// input). Sub-frame remainder is buffered for the next call.
    pub(crate) fn process(&mut self, pcm: &[i16]) -> Vec<i16> {
        self.pending.extend_from_slice(pcm);
        let count = self.pending.len() / OUT_FRAME * OUT_FRAME;
        if count == 0 {
            return Vec::new();
        }
        let tail = self.pending.split_off(count);
        let chunk = std::mem::replace(&mut self.pending, tail);
        let mut out = Vec::with_capacity(chunk.len());
        for frame in chunk.chunks_exact(OUT_FRAME) {
            // 24 kHz -> 48 kHz
            let up = self
                .up
                .remix_and_resample(frame, OUT_FRAME as u32, 1, OUT_RATE, 1, RN_RATE);
            if up.len() != RN_FRAME {
                out.extend_from_slice(frame);
                continue;
            }
            // Preserve PCM16 amplitude scale (RNNoise expects that, not [-1,1]).
            let mut input = [0.0f32; RN_FRAME];
            for (target, sample) in input.iter_mut().zip(up.iter()) {
                *target = f32::from(*sample);
            }
            let mut filtered = [0.0f32; RN_FRAME];
            let _ = self.denoiser.process(&input, &mut filtered);
            let mut pcm48 = [0i16; RN_FRAME];
            for (target, sample) in pcm48.iter_mut().zip(filtered.iter()) {
                *target = sample.round().clamp(-32768.0, 32767.0) as i16;
            }
            // 48 kHz -> 24 kHz
            let down =
                self.down
                    .remix_and_resample(&pcm48, RN_FRAME as u32, 1, RN_RATE, 1, OUT_RATE);
            if down.len() == OUT_FRAME {
                out.extend_from_slice(&down);
            } else {
                out.extend_from_slice(frame);
            }
        }
        out
    }

    /// Flush any buffered sub-frame tail, unprocessed (a few ms at most).
    pub(crate) fn finish(&mut self) -> Vec<i16> {
        std::mem::take(&mut self.pending)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tone(samples: usize, hz: f64) -> Vec<i16> {
        (0..samples)
            .map(|i| ((i as f64 * hz * std::f64::consts::TAU / OUT_RATE as f64).sin() * 8000.0) as i16)
            .collect()
    }

    #[test]
    fn preserves_duration_for_frame_aligned_input() {
        let mut d = OutputDenoise::new().expect("rnnoise available");
        let pcm = tone(2400, 300.0);
        let out = d.process(&pcm);
        assert_eq!(out.len(), pcm.len());
        assert!(out.iter().all(|s| *s > -32768));
    }

    #[test]
    fn arbitrary_partitions_keep_total_length() {
        let pcm = tone(2400 + 137, 220.0);
        let mut whole = OutputDenoise::new().expect("rnnoise available");
        let mut a = whole.process(&pcm);
        a.extend(whole.finish());
        let mut split = OutputDenoise::new().expect("rnnoise available");
        let mut b = Vec::new();
        for part in pcm.chunks(137) {
            b.extend(split.process(part));
        }
        b.extend(split.finish());
        assert_eq!(a.len(), b.len());
    }
}
