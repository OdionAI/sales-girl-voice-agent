"""Per-call noise suppression before VAD/ASR; PCM16 mono at 16 kHz.

Implementation is operator-selected per call:
- ``webrtc`` (default): LiveKit WebRTC APM, the established filter.
- ``rnnoise``: RNNoise (recurrent-neural-network suppressor via ``pyrnnoise``),
  which also reports a per-frame speech probability used only for metrics here.

Both paths fail open: on any error the filter disables itself and passes the
caller's audio through unfiltered, and the trace records the failure.
"""
import math
import os
import time
from array import array

from livekit import rtc


class InputNoiseFilter:
    BLOCK = 320  # 10 ms at 16 kHz; WebRTC APM requires exactly this.

    def __init__(self, context, trace):
        self.trace = trace
        self.pending = bytearray()
        self.frames = self.input_energy = self.output_energy = self.samples = 0
        self.processing_ms = 0.0
        self.speech_prob_total = 0.0
        self.speech_prob_frames = 0
        allowed = {s.strip() for s in os.getenv('RVC_INPUT_NOISE_AGENT_IDS', '').split(',') if s.strip()}
        self.enabled = os.getenv('RVC_INPUT_NOISE_SUPPRESSION') == '1' and ('*' in allowed or context.agent_id in allowed)
        self.implementation = str(os.getenv('RVC_INPUT_NOISE_IMPLEMENTATION', 'webrtc')).strip().lower()
        self.apm = None
        self.rnnoise = None
        if self.enabled:
            if self.implementation == 'rnnoise':
                self._init_rnnoise()
            else:
                self.implementation = 'webrtc'
                self._init_webrtc()
        trace.record('input_noise_filter_ready', enabled=self.enabled,
                     implementation=self.implementation if self.enabled else 'off',
                     sample_rate=16000, frame_ms=10, echo_cancellation=False, auto_gain_control=False)

    def _init_webrtc(self):
        try:
            self.apm = rtc.AudioProcessingModule(noise_suppression=True,
                high_pass_filter=False, echo_cancellation=False, auto_gain_control=False)
        except Exception as exc:
            self.enabled = False
            self.trace.record('input_noise_filter_error', stage='init', implementation='webrtc',
                              error_type=type(exc).__name__, fallback='unfiltered')

    def _init_rnnoise(self):
        try:
            from pyrnnoise import RNNoise
            self.rnnoise = RNNoise(sample_rate=16000)
        except Exception as exc:
            self.enabled = False
            self.rnnoise = None
            self.trace.record('input_noise_filter_error', stage='init', implementation='rnnoise',
                              error_type=type(exc).__name__, fallback='unfiltered')

    def process(self, pcm):
        if not self.enabled:
            return pcm
        if self.rnnoise is not None:
            return self._process_rnnoise(pcm)
        return self._process_apm(pcm)

    def _process_apm(self, pcm):
        self.pending.extend(pcm)
        size = len(self.pending) // self.BLOCK * self.BLOCK
        if not size:
            return b''
        raw = bytes(self.pending[:size]); del self.pending[:size]
        output = bytearray()
        started = time.perf_counter()
        try:
            for pos in range(0, size, self.BLOCK):
                frame = rtc.AudioFrame(bytearray(raw[pos:pos+self.BLOCK]),16000,1,160)
                self.apm.process_stream(frame)
                output.extend(frame.data.cast('B'))
        except Exception as exc:
            # Never lose caller audio or continue claiming suppression after failure.
            self.enabled = False
            self.trace.record('input_noise_filter_error', stage='process', implementation='webrtc',
                              error_type=type(exc).__name__, fallback='unfiltered')
            raw += bytes(self.pending); self.pending.clear()
            return raw
        self.processing_ms += (time.perf_counter()-started)*1000
        self._accumulate(raw, bytes(output), size // self.BLOCK)
        return bytes(output)

    def _process_rnnoise(self, pcm):
        import numpy as np
        started = time.perf_counter()
        output = bytearray()
        try:
            for probability, frame in self.rnnoise.denoise_chunk(
                    np.frombuffer(pcm, dtype=np.int16), partial=False):
                output.extend(np.asarray(frame, dtype=np.int16).reshape(-1).tobytes())
                self.speech_prob_total += float(np.asarray(probability).reshape(-1)[0])
                self.speech_prob_frames += 1
        except Exception as exc:
            # Fail open: drop suppression entirely rather than corrupt or drop audio.
            self.enabled = False
            self.rnnoise = None
            self.trace.record('input_noise_filter_error', stage='process', implementation='rnnoise',
                              error_type=type(exc).__name__, fallback='unfiltered')
            return pcm
        self.processing_ms += (time.perf_counter()-started)*1000
        self._accumulate(pcm, bytes(output), max(1, len(pcm) // self.BLOCK))
        return bytes(output)

    def _accumulate(self, raw, output, frame_units):
        self.frames += frame_units
        before = array('h', raw)
        after = array('h', output[:(len(raw) // 2) * 2])
        self.samples += len(before)
        self.input_energy += sum(x*x for x in before)
        self.output_energy += sum(x*x for x in after)
        if self.frames >= 500:
            self.report()

    def report(self):
        if not self.samples:
            return
        fields = dict(frames=self.frames, samples=self.samples,
            input_rms=round(math.sqrt(self.input_energy/self.samples),2),
            output_rms=round(math.sqrt(self.output_energy/self.samples),2),
            processing_ms=round(self.processing_ms,2))
        if self.speech_prob_frames:
            fields['speech_prob'] = round(self.speech_prob_total/self.speech_prob_frames, 3)
        self.trace.record('input_noise_filter_metrics', **fields)
        self.frames = self.input_energy = self.output_energy = self.samples = 0
        self.speech_prob_total = self.speech_prob_frames = 0
        self.processing_ms = 0.0

    def finish(self):
        # Flush a partial APM block without extending the stream duration. The
        # RNNoise path holds at most a sub-frame of resampler state at session
        # end, so nothing caller-audible is dropped.
        count=len(self.pending)
        if not count:
            self.report(); return b''
        output=self.process(bytes(self.BLOCK-count))[:count]
        self.report()
        return output


class ListeningNoiseRoute:
    """Keep operator suppression unless Speex or raw-level detection owns input.

    The worker calls process under the pipeline input lock. RPC mode changes are
    accepted only between utterances. Recreate the operator filter on returning
    to standard so its adaptation/buffers cannot contain an earlier utterance.
    An explicit level threshold requires original PCM, before operator filtering;
    timing-only tuning retains the operator filter. Client preprocessing remains.
    """

    def __init__(self, context, trace):
        self.context, self.trace = context, trace
        self.filter = InputNoiseFilter(context, trace)
        self.bypassing = False

    def process(self, pcm, *, noisy_environment=False, raw_level_detection=False):
        if noisy_environment or raw_level_detection:
            if not self.bypassing:
                self.filter.report()
            self.bypassing = True
            # ListeningInput owns denoising and/or raw-level speech detection.
            return pcm
        if self.bypassing:
            self.filter = InputNoiseFilter(self.context, self.trace)
            self.bypassing = False
        return self.filter.process(pcm)

    def finish(self):
        if self.bypassing:
            return b''
        return self.filter.finish()
