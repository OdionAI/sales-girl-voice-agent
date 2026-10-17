from __future__ import annotations

import asyncio
import json
import itertools
import math
import random
import struct
import tempfile
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from livekit import rtc

from latency_lab.config import LabConfig
from latency_lab.listening import (
    FRAME_BYTES, RPC_METHOD, ListeningBusy, ListeningConfiguration, ListeningInput,
    ListeningUnavailable, SpeexNoiseSuppressor,
)
from latency_lab.livekit_worker import register_listening_rpc
from latency_lab.pipeline import ConversationPipeline, rms_pcm16
from latency_lab.trace import TraceRecorder


def pcm_samples(values):
    return struct.pack(f"<{len(values)}h", *values)


def fixture():
    with wave.open(str(Path(__file__).parent / "fixtures" / "short-answers.wav"), "rb") as source:
        assert (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (1, 2, 16000)
        pcm = source.readframes(source.getnframes())
    # A trailing full silence window allows the real VAD's hangover to settle.
    pcm += b"\0" * 32000
    return pcm + b"\0" * (-len(pcm) % FRAME_BYTES)


class ListeningConfigurationTests(unittest.TestCase):
    def test_strict_protocol_round_trip(self):
        for noise in ("standard", "noisyEnvironment"):
            for sensitivity in ("responsive", "balanced", "strict"):
                configuration = ListeningConfiguration(noise, sensitivity)
                self.assertEqual(ListeningConfiguration.parse(json.dumps(configuration.payload())), configuration)

    def test_rejects_malformed_unknown_and_ambiguous_configurations(self):
        valid = ListeningConfiguration().payload()
        invalid = ["[]", "null", "{", "{}", "x" * 1025,
                   '{"version":1,"version":1,"noiseControl":"standard","responseSensitivity":"balanced"}']
        for changes in ({"version": True}, {"version": 1.0}, {"version": 2},
                        {"noiseControl": "isolation"}, {"noiseControl": []},
                        {"responseSensitivity": "maximum"}, {"responseSensitivity": None},
                        {"extra": "field"}):
            invalid.append(json.dumps({**valid, **changes}))
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                ListeningConfiguration.parse(payload)

    def test_baseline_is_lazy_and_exact_even_without_native_dependencies(self):
        with patch("ctypes.util.find_library", return_value=None), patch.dict("sys.modules", {"webrtcvad": None}):
            listening = ListeningInput()
            listening.configure(ListeningConfiguration())
            pcm = pcm_samples([0, 20, -30, 32767, -32768])
            self.assertEqual(listening.process(pcm)[0].pcm, pcm)
            self.assertIsNone(listening.process(pcm)[0].speech)
            with self.assertRaises(ListeningUnavailable):
                listening.configure(ListeningConfiguration("noisyEnvironment"))
            self.assertTrue(listening.configuration.is_baseline)

    def test_failed_native_setup_does_not_publish_effective_settings(self):
        listening = ListeningInput()
        with patch("latency_lab.listening.SpeexNoiseSuppressor", side_effect=ListeningUnavailable("missing")):
            with self.assertRaises(ListeningUnavailable):
                listening.configure(ListeningConfiguration("noisyEnvironment"))
        self.assertTrue(listening.configuration.is_baseline)
        listening.close()

    def test_profiles_keep_baseline_and_allow_short_answers(self):
        self.assertEqual(ListeningConfiguration().confirmation_ms(240, interrupting=False), 240)
        for sensitivity in ("responsive", "balanced", "strict"):
            config = ListeningConfiguration("noisyEnvironment", sensitivity)
            self.assertLessEqual(config.confirmation_ms(240, interrupting=False), 120)
        self.assertEqual(ListeningConfiguration("standard", "strict").confirmation_ms(240, interrupting=True), 360)


class NoiseProcessingTests(unittest.TestCase):
    def test_real_speech_and_quiet_short_answers_are_classified(self):
        original = fixture()
        values = struct.unpack(f"<{len(original) // 2}h", original)
        for gain in (1.0, 0.12):
            for profile in (ListeningConfiguration("standard", "strict"),
                            ListeningConfiguration("noisyEnvironment", "balanced")):
                with self.subTest(gain=gain, profile=profile):
                    listening = ListeningInput()
                    listening.configure(profile)
                    try:
                        pcm = pcm_samples([round(value * gain) for value in values])
                        frames = listening.process(pcm)
                        self.assertEqual(sum(len(frame.pcm) for frame in frames), len(pcm))
                        # Spoken yes/no/stop, not a synthetic sine passed off as speech.
                        runs = [len(list(group)) for speech, group in
                                itertools.groupby(frame.speech for frame in frames) if speech]
                        self.assertEqual(len(runs), 3)
                        self.assertTrue(all(length >= 6 for length in runs))
                    finally:
                        listening.close()

    def test_quiet_short_answers_in_five_and_ten_db_noise_keep_speech_onset(self):
        import webrtcvad
        clean = fixture()
        values = struct.unpack(f"<{len(clean) // 2}h", clean)
        active = [sample for offset in range(0, len(clean), FRAME_BYTES)
                  if rms_pcm16(clean[offset:offset + FRAME_BYTES]) > 500
                  for sample in struct.unpack("<320h", clean[offset:offset + FRAME_BYTES])]
        speech_rms = math.sqrt(sum(value * value for value in active) / len(active))
        for snr_db in (5, 10):
            randomizer = random.Random(42)
            noise_rms = speech_rms * 0.12 / 10 ** (snr_db / 20)
            mixed = pcm_samples([max(-32768, min(32767, round(
                value * 0.12 + randomizer.gauss(0, noise_rms)))) for value in values])
            listening = ListeningInput()
            listening.configure(ListeningConfiguration("noisyEnvironment", "strict"))
            try:
                frames = listening.process(mixed)
                raw_vad = webrtcvad.Vad(2)
                raw_speech_frames = sum(raw_vad.is_speech(mixed[offset:offset + FRAME_BYTES], 16000)
                                        for offset in range(0, len(mixed), FRAME_BYTES))
                voiced = [frame.speech for frame in frames]
                runs = [len(list(group)) for speech, group in itertools.groupby(voiced) if speech]
                self.assertEqual(len(runs), 3)
                self.assertTrue(all(length >= 6 for length in runs))
                self.assertGreaterEqual(sum(voiced), raw_speech_frames * 0.9)
                self.assertLessEqual(voiced.index(True) * 20, 60)  # Within the retained pre-roll.
                self.assertEqual(sum(len(frame.pcm) for frame in frames), len(mixed))
            finally:
                listening.close()

    def test_stationary_noise_reduced_without_dropping_samples_or_clipping(self):
        randomizer = random.Random(41)
        pcm = pcm_samples([round(randomizer.gauss(0, 900)) for _ in range(16000 * 4)])
        reducer = SpeexNoiseSuppressor()
        try:
            output = b"".join(reducer.process(pcm[i:i + FRAME_BYTES]) for i in range(0, len(pcm), FRAME_BYTES))
        finally:
            reducer.close()
        self.assertEqual(len(output), len(pcm))
        # Ignore adaptation and compare the last second of stationary noise.
        reduction_db = 20 * math.log10(rms_pcm16(output[-32000:]) / rms_pcm16(pcm[-32000:]))
        self.assertLess(reduction_db, -3.0)
        self.assertNotIn(32767, struct.unpack(f"<{len(output) // 2}h", output))

    def test_noise_adaptation_and_clap_do_not_confirm_strict_barge_in(self):
        randomizer = random.Random(41)
        noise = pcm_samples([round(randomizer.gauss(0, 900)) for _ in range(64000)])
        clap = pcm_samples([0] * 32000 + [20000] * 20 + [-20000] * 20 + [0] * 31960)
        for pcm in (noise, clap):
            listening = ListeningInput()
            listening.configure(ListeningConfiguration("noisyEnvironment", "strict"))
            try:
                voiced_runs = [len(list(group)) * 20 for speech, group in
                               itertools.groupby(frame.speech for frame in listening.process(pcm)) if speech]
                self.assertTrue(all(duration < 360 for duration in voiced_runs))
            finally:
                listening.close()

    def test_irregular_input_and_profile_switch_preserve_all_samples(self):
        listening = ListeningInput()
        listening.configure(ListeningConfiguration("standard", "responsive"))
        pcm = fixture()
        outputs = []
        # Neither packet boundaries nor a partial final frame may discard speech.
        for i in range(0, len(pcm), 146):
            outputs.extend(frame.pcm for frame in listening.process(pcm[i:i + 146]))
        listening.configure(ListeningConfiguration())
        outputs.extend(frame.pcm for frame in listening.process(b""))
        self.assertEqual(b"".join(outputs), pcm)
        listening.close()


class ListeningPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.pipeline = ConversationPipeline(LabConfig(), TraceRecorder(Path(self.temp.name)), AsyncMock())
        self.pipeline.stt = SimpleNamespace(append=AsyncMock(), commit=AsyncMock(), close=AsyncMock())

    async def asyncTearDown(self):
        await self.pipeline.close()
        self.temp.cleanup()

    async def frame(self, pcm, speech):
        await self.pipeline._audio_frame(pcm, speech=speech, client_sent_ms=None, client_network_ms=None)

    async def test_initial_soft_consonants_and_all_candidate_frames_reach_batch_asr(self):
        self.pipeline.configure_listening(ListeningConfiguration("standard", "responsive"))
        frames = [pcm_samples([index + 1] * 320) for index in range(7)]
        for frame in frames[:3]:
            await self.frame(frame, False)  # VAD misses a quiet beginning.
        for frame in frames[3:]:
            await self.frame(frame, True)  # 80 ms is sufficient in responsive mode.
        silence = b"\0" * FRAME_BYTES
        for _ in range(15):
            await self.frame(silence, False)
        self.pipeline.stt.commit.assert_awaited_once_with(b"".join(frames) + silence * 15)
        self.assertEqual(self.pipeline.state, "awaiting_final")

    async def test_completed_turn_does_not_leave_old_speech_in_next_preroll(self):
        self.pipeline.configure_listening(ListeningConfiguration("standard", "responsive"))
        self.pipeline.config = replace(self.pipeline.config, end_silence_ms=60)
        voiced = pcm_samples([200] * 320)
        silence = b"\0" * FRAME_BYTES
        for _ in range(4):
            await self.frame(voiced, True)
        for _ in range(3):
            await self.frame(silence, False)
        self.pipeline.stt.commit.assert_awaited_once()
        # Even with an end-silence shorter than pre-roll, the committed utterance
        # cannot become the beginning of the next independently owned turn.
        self.assertEqual(self.pipeline._listening_preroll, bytearray())

    async def test_noise_reduction_is_before_streaming_and_batch_asr(self):
        self.pipeline.configure_listening(ListeningConfiguration("noisyEnvironment", "responsive"))
        # Deterministic boundary test: native denoise quality has a separate test.
        input_pcm = pcm_samples([800] * 320)
        output_pcm = pcm_samples([400] * 320)
        self.pipeline.listening._denoiser.process = Mock(return_value=output_pcm)
        self.pipeline.listening._vad = SimpleNamespace(is_speech=lambda pcm, rate: pcm == output_pcm)
        self.pipeline.listening._raw_vad = SimpleNamespace(is_speech=lambda pcm, rate: True)
        for _ in range(4):
            await self.pipeline.audio(input_pcm)
        self.assertEqual(bytes(self.pipeline.turn_pcm16), output_pcm * 4)
        self.assertEqual([call.args[0] for call in self.pipeline.stt.append.await_args_list], [output_pcm] * 4)

    async def test_strict_barge_in_rejects_short_burst_and_accepts_sustained_speech(self):
        self.pipeline.configure_listening(ListeningConfiguration("standard", "strict"))
        self.pipeline.state = "playing"
        self.pipeline._cancel_attempt = AsyncMock()
        voiced = pcm_samples([200] * 320)  # Below old RMS threshold: qualified quiet voice.
        for _ in range(12):
            await self.frame(voiced, True)
        for _ in range(3):
            await self.frame(b"\0" * FRAME_BYTES, False)
        self.assertEqual(self.pipeline.state, "playing")
        self.pipeline._cancel_attempt.assert_not_awaited()
        for _ in range(18):
            await self.frame(voiced, True)
        self.pipeline._cancel_attempt.assert_awaited_once_with("user_barge_in")
        self.assertEqual(self.pipeline.state, "speaking")

    async def test_native_noise_and_clap_do_not_interrupt_agent_playback(self):
        self.pipeline.configure_listening(ListeningConfiguration("noisyEnvironment", "strict"))
        self.pipeline.state = "playing"
        self.pipeline._cancel_attempt = AsyncMock()
        randomizer = random.Random(41)
        noise = pcm_samples([round(randomizer.gauss(0, 900)) for _ in range(64000)])
        clap = pcm_samples([0] * 32000 + [20000] * 20 + [-20000] * 20 + [0] * 31960)
        for pcm in (noise, clap):
            for offset in range(0, len(pcm), 3200):
                await self.pipeline.audio(pcm[offset:offset + 3200])
        self.pipeline._cancel_attempt.assert_not_awaited()
        self.assertEqual(self.pipeline.state, "playing")
        self.pipeline.stt.commit.assert_not_awaited()
        self.assertEqual(sum(len(call.args[0]) for call in self.pipeline.stt.append.await_args_list), len(noise + clap))

    async def test_response_change_does_not_reinterpret_in_progress_candidate(self):
        self.pipeline.configure_listening(ListeningConfiguration("standard", "strict"))
        self.pipeline.state = "playing"
        self.pipeline._cancel_attempt = AsyncMock()
        voiced = pcm_samples([200] * 320)
        await self.frame(voiced, True)
        with self.assertRaises(ListeningBusy):
            self.pipeline.configure_listening(ListeningConfiguration("standard", "responsive"))
        self.assertEqual(self.pipeline.listening.configuration.response_sensitivity, "strict")
        # Re-applying the already-effective settings is harmless while speaking.
        self.pipeline.configure_listening(ListeningConfiguration("standard", "strict"))
        for _ in range(16):
            await self.frame(voiced, True)
        self.pipeline._cancel_attempt.assert_not_awaited()
        await self.frame(voiced, True)
        self.pipeline._cancel_attempt.assert_awaited_once()
        self.assertEqual(self.pipeline.config.min_speech_ms, 240)

    async def test_speech_fixture_can_complete_a_turn_with_enhancement(self):
        self.pipeline.configure_listening(ListeningConfiguration("noisyEnvironment", "balanced"))
        pcm = fixture()
        for offset in range(0, len(pcm), 3200):
            await self.pipeline.audio(pcm[offset:offset + 3200])
        self.pipeline.stt.commit.assert_awaited_once()
        self.assertGreater(len(self.pipeline.stt.commit.await_args.args[0]), 3200)
        self.assertEqual(sum(len(call.args[0]) for call in self.pipeline.stt.append.await_args_list), len(pcm))


class ListeningRPCTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.participant = SimpleNamespace(register_rpc_method=Mock())
        self.pipeline = SimpleNamespace(configure_listening=Mock())
        self.ready = asyncio.Event()
        self.ready.set()
        self.lock = asyncio.Lock()
        self.handler = register_listening_rpc(self.participant, self.pipeline,
                                             caller_identity="intended-caller", ready=self.ready,
                                             input_lock=self.lock)

    def request(self, payload=None, identity="intended-caller"):
        return rtc.RpcInvocationData("request", identity,
                                     payload if payload is not None else json.dumps(ListeningConfiguration().payload()), 5)

    async def test_acknowledges_exact_effective_contract(self):
        self.participant.register_rpc_method.assert_called_once_with(RPC_METHOD, self.handler)
        config = ListeningConfiguration("noisyEnvironment", "strict")
        self.assertEqual(json.loads(await self.handler(self.request(json.dumps(config.payload())))), config.payload())
        self.pipeline.configure_listening.assert_called_once_with(config)

    async def test_rejects_other_participant_readiness_and_malformed(self):
        for request, code in ((self.request(identity="intruder"), 403), (self.request("[]"), 400)):
            with self.subTest(code=code), self.assertRaises(rtc.RpcError) as result:
                await self.handler(request)
            self.assertEqual(result.exception.code, code)
        self.ready.clear()
        with self.assertRaises(rtc.RpcError) as result:
            await self.handler(self.request())
        self.assertEqual(result.exception.code, 503)
        self.pipeline.configure_listening.assert_not_called()

    async def test_dependency_failure_is_not_a_success_acknowledgment(self):
        self.pipeline.configure_listening.side_effect = ListeningUnavailable("missing SpeexDSP")
        with self.assertRaises(rtc.RpcError) as result:
            await self.handler(self.request())
        self.assertEqual(result.exception.code, 503)

    async def test_active_speech_rejection_is_retryable_and_not_an_ack(self):
        self.pipeline.configure_listening.side_effect = ListeningBusy("Pause speaking and try again.")
        with self.assertRaises(rtc.RpcError) as result:
            await self.handler(self.request())
        self.assertEqual(result.exception.code, 409)
        self.assertEqual(result.exception.message, "Pause speaking and try again.")

    async def test_queued_configuration_cannot_apply_after_shutdown(self):
        async with self.lock:
            config = ListeningConfiguration("noisyEnvironment", "strict")
            task = asyncio.create_task(self.handler(self.request(json.dumps(config.payload()))))
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            # Worker shutdown clears readiness before releasing/cancelling input.
            self.ready.clear()
        with self.assertRaises(rtc.RpcError) as result:
            await task
        self.assertEqual(result.exception.code, 503)
        self.pipeline.configure_listening.assert_not_called()

    async def test_update_waits_for_audio_block_lock(self):
        async with self.lock:
            task = asyncio.create_task(self.handler(self.request()))
            await asyncio.sleep(0)
            self.pipeline.configure_listening.assert_not_called()
            self.assertFalse(task.done())
        await task
        self.pipeline.configure_listening.assert_called_once()
