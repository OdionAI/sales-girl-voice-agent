from __future__ import annotations

import asyncio
import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp

from latency_lab.pipeline import Attempt, ConversationPipeline, PhraseTokenizer, normalize, rms_pcm16
from latency_lab.config import (
    LabConfig,
    _url,
    apply_runtime_overrides,
    derive_stt_batch_url,
    lab_config_from_environ,
    normalize_llm_base_url,
)
from latency_lab.providers import (
    RLLMClient,
    RSTTClient,
    clean_transcript,
    frame_pcm16_chunks,
    pcm16_wav,
)
from latency_lab.report import generate_report
from latency_lab.trace import TraceRecorder


class PipelineHelpersTest(unittest.TestCase):
    def test_lab_config_from_environ_reads_current_process_env(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "AGENT_CONFIG_SERVICE_BASE_URL": "https://config.example/",
                "CONVERSATION_SERVICE_TOKEN": "secret",
                "KNOWLEDGE_SERVICE_BASE_URL": "https://kb.example/",
            },
            clear=True,
        ):
            config = lab_config_from_environ()
            self.assertEqual(config.agent_config_api_base_url, "https://config.example")
            self.assertEqual(config.agent_config_service_token, "secret")
            self.assertEqual(config.knowledge_service_base_url, "https://kb.example")

    def test_platform_agent_config_url_is_used_as_fallback(self) -> None:
        with patch.dict(
            "os.environ",
            {"AGENT_CONFIG_SERVICE_BASE_URL": "https://config.example/v1/"},
            clear=True,
        ):
            self.assertEqual(
                _url("AGENT_CONFIG_API_BASE_URL", "AGENT_CONFIG_SERVICE_BASE_URL"),
                "https://config.example/v1",
            )

    def test_experiment_agent_config_url_overrides_platform_url(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "AGENT_CONFIG_API_BASE_URL": "https://override.example/",
                "AGENT_CONFIG_SERVICE_BASE_URL": "https://config.example/",
            },
            clear=True,
        ):
            self.assertEqual(
                _url("AGENT_CONFIG_API_BASE_URL", "AGENT_CONFIG_SERVICE_BASE_URL"),
                "https://override.example",
            )

    def test_tts_initial_chunk_experiment_defaults_to_sixteen_frames(self) -> None:
        self.assertEqual(LabConfig().tts_initial_codec_chunk_frames, 16)

    def test_end_of_turn_experiment_defaults_to_three_hundred_ms(self) -> None:
        self.assertEqual(LabConfig().end_silence_ms, 300.0)

    def test_cached_helen_icl_voice_is_the_default(self) -> None:
        self.assertEqual(LabConfig().tts_voice, "helen-mavino-0030")

    def test_qwen_model_id_matches_gateway_advertisement(self) -> None:
        self.assertEqual(LabConfig().llm_model, "qwen3.8-27b")

    def test_voice_lab_overrides_remap_model_endpoints(self) -> None:
        config = apply_runtime_overrides(
            LabConfig(),
            {
                "llm_model": "qwen3.8_27b",
                "llm_base_url": "http://102.88.137.124:8080/qwen38-standard/v1/chat/completions",
                "llm_disable_thinking": "true",
                "stt_model": "Qwen3-ASR",
                "stt_base_url": "ws://102.88.137.124:8080/asr-rt/v1/realtime",
                "tts_model": "Qwen3-TTS",
                "tts_base_url": "http://102.88.137.124:8080/tts/v1/audio/speech",
                "tts_voice_id": "uyai-voice-2",
            },
            language="fr",
        )
        self.assertEqual(config.llm_model, "qwen3.8_27b")
        self.assertEqual(
            config.llm_base_url,
            "http://102.88.137.124:8080/qwen38-standard/v1",
        )
        self.assertFalse(config.llm_enable_thinking)
        self.assertEqual(config.stt_ws_url, "ws://102.88.137.124:8080/asr-rt/v1/realtime")
        self.assertEqual(
            config.stt_batch_url,
            "http://102.88.137.124:8080/asr-rt/v1/audio/transcriptions",
        )
        self.assertEqual(config.tts_language, "French")
        self.assertEqual(config.tts_voice, "uyai-voice-2")
        self.assertEqual(
            normalize_llm_base_url("http://host/v1"),
            "http://host/v1",
        )
        self.assertEqual(
            derive_stt_batch_url("wss://host/asr-rt/v1/realtime"),
            "https://host/asr-rt/v1/audio/transcriptions",
        )

    def test_phrase_tokenizer_emits_complete_phrases_and_tail(self) -> None:
        tokenizer = PhraseTokenizer()
        self.assertEqual(tokenizer.push("Hello there. How are"), ["Hello there."])
        self.assertEqual(tokenizer.push(" you?"), ["How are you?"])
        self.assertEqual(tokenizer.finish(), [])

    def test_transcript_cleanup_handles_stacked_ascend_segments(self) -> None:
        raw = "language English<asr_text>Hello.\nlanguage English<asr_text>How are you?"
        self.assertEqual(clean_transcript(raw), "Hello. How are you?")

    def test_transcript_cleanup_rejects_wrong_language_hypothesis(self) -> None:
        self.assertEqual(clean_transcript("language Chinese<asr_text>嗯。"), "")

    def test_normalization_and_rms(self) -> None:
        self.assertEqual(normalize("Hello, WORLD!"), "hello world")
        self.assertEqual(rms_pcm16(b"\x00\x00" * 20), 0.0)


class AudioFramingTest(unittest.IsolatedAsyncioTestCase):
    async def test_pcm16_odd_transport_chunks_are_carried_not_dropped(self) -> None:
        async def source():
            for chunk in [b"\x01", b"\x02\x03", b"\x04"]:
                yield chunk

        framed = [chunk async for chunk in frame_pcm16_chunks(source())]

        self.assertEqual(b"".join(framed), b"\x01\x02\x03\x04")


class LLMGatewayRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_retryable_gateway_failures_recover_before_any_tokens_emit(self) -> None:
        class FakeContent:
            async def iter_any(self):
                yield (
                    b'data: {"choices":[{"delta":{"content":"Recovered"}}]}\n\n'
                    b'data: [DONE]\n\n'
                )

        class FakeResponse:
            def __init__(self, status):
                self.status = status
                self.content = FakeContent()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def read(self):
                return b""

            def raise_for_status(self):
                if self.status >= 400:
                    raise AssertionError(f"unexpected terminal HTTP {self.status}")

        class FakeSession:
            def __init__(self):
                self.request_count = 0

            def post(self, *_args, **_kwargs):
                self.request_count += 1
                return FakeResponse(502 if self.request_count < 3 else 200)

        session = FakeSession()
        client = RLLMClient(LabConfig(llm_base_url="http://llm.test/v1"), session)
        with patch.object(client, "_retry_delays_seconds", (0.0, 0.0, 0.0)):
            output = [
                item
                async for item in client.stream(
                    [{"role": "user", "content": "Hello"}]
                )
            ]

        self.assertEqual(output, ["Recovered"])
        self.assertEqual(session.request_count, 3)


@unittest.skip("superseded by batch-authoritative HybridSTTTest coverage")
class STTRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_initial_connect_failure_enters_supervisor_without_failing_call(self) -> None:
        async def ignore_partial(_text, _raw):
            return None

        async def ignore_final(_text):
            return None

        events = []
        client = RSTTClient(
            LabConfig(),
            ignore_partial,
            ignore_final,
            lambda event, data: events.append((event, data)),
        )
        client._open_socket = AsyncMock(side_effect=OSError("gateway unavailable"))
        client.ensure_recovery = Mock()

        await client.connect()

        client.ensure_recovery.assert_called_once_with("initial_connect_failed")
        self.assertIn("stt_initial_connect_degraded", [event for event, _ in events])
        await client.close()

    async def test_healthy_audio_uses_direct_socket_without_recovery_or_buffering(self) -> None:
        async def ignore_partial(_text, _raw):
            return None

        async def ignore_final(_text):
            return None

        class FakeSocket:
            closed = False

            def __init__(self):
                self.messages = []

            async def send_json(self, message):
                self.messages.append(message)

            async def close(self):
                self.closed = True

        client = RSTTClient(
            LabConfig(), ignore_partial, ignore_final, lambda _event, _data: None
        )
        socket = FakeSocket()
        client.ws = socket
        client.ensure_recovery = Mock()

        await client.append(b"\x01\x00" * 1600)

        self.assertEqual(len(socket.messages), 1)
        self.assertEqual(socket.messages[0]["type"], "input_audio_buffer.append")
        self.assertEqual(client._audio_backlog_bytes, 0)
        client.ensure_recovery.assert_not_called()
        await client.close()

    async def test_provider_recovery_replaces_socket_and_resets_decoder(self) -> None:
        events = []

        async def ignore_partial(_text, _raw):
            return None

        async def ignore_final(_text):
            return None

        class FakeSocket:
            closed = False

            def __init__(self):
                self.messages = []

            async def close(self):
                self.closed = True

            async def send_json(self, message):
                self.messages.append(message)

        client = RSTTClient(
            LabConfig(),
            ignore_partial,
            ignore_final,
            lambda event, data: events.append((event, data)),
        )
        old_socket = FakeSocket()
        new_socket = FakeSocket()
        old_task = asyncio.create_task(asyncio.Event().wait())
        client.ws = old_socket
        client.recv_task = old_task
        client.accumulated = "stale transcript"
        client._in_language_preamble = True
        client._drop_hypothesis = True
        async def open_socket():
            client.ws = new_socket

        client._open_socket = AsyncMock(side_effect=open_socket)

        await client.recover("final_timeout")

        self.assertTrue(old_socket.closed)
        self.assertTrue(old_task.cancelled())
        self.assertEqual(client.accumulated, "")
        self.assertFalse(client._in_language_preamble)
        self.assertFalse(client._drop_hypothesis)
        client._open_socket.assert_awaited_once()
        self.assertEqual(events[0][0], "stt_recovery_start")
        self.assertEqual(events[-1][0], "stt_recovery_complete")
        await client.close()

    async def test_disconnected_audio_is_buffered_without_blocking_for_reconnect(self) -> None:
        async def ignore_partial(_text, _raw):
            return None

        async def ignore_final(_text):
            return None

        client = RSTTClient(
            LabConfig(), ignore_partial, ignore_final, lambda _event, _data: None
        )
        client.ensure_recovery = Mock()

        await client.append(b"\x01\x00" * 1600)

        self.assertEqual(client._audio_backlog_bytes, 3200)
        client.ensure_recovery.assert_called_once_with(
            "audio_arrived_while_disconnected"
        )
        await client.close()

    async def test_recovery_audio_buffer_is_strictly_bounded(self) -> None:
        async def ignore_partial(_text, _raw):
            return None

        async def ignore_final(_text):
            return None

        client = RSTTClient(
            LabConfig(stt_recovery_buffer_seconds=0.1),
            ignore_partial,
            ignore_final,
            lambda _event, _data: None,
        )
        client.ensure_recovery = Mock()

        await client.append(b"\x01\x00" * 1600)
        await client.append(b"\x02\x00" * 1600)

        self.assertEqual(client._max_backlog_bytes, 3200)
        self.assertLessEqual(client._audio_backlog_bytes, 3200)
        self.assertLessEqual(client._turn_audio_shadow_bytes, 3200)
        await client.close()

    async def test_recovery_replays_buffer_then_pending_final_commit(self) -> None:
        events = []

        async def ignore_partial(_text, _raw):
            return None

        async def ignore_final(_text):
            return None

        class FakeSocket:
            closed = False

            def __init__(self):
                self.messages = []

            async def send_json(self, message):
                self.messages.append(message)

            async def close(self):
                self.closed = True

        client = RSTTClient(
            LabConfig(),
            ignore_partial,
            ignore_final,
            lambda event, data: events.append((event, data)),
        )
        socket = FakeSocket()
        client._remember_turn_audio(b"\x01\x00" * 800)
        client._seed_backlog_from_turn_audio()
        client._pending_final_commit = True

        async def open_socket():
            client.ws = socket

        client._open_socket = AsyncMock(side_effect=open_socket)

        await client.recover("provider_socket_closed")

        self.assertEqual(
            [message["type"] for message in socket.messages],
            ["input_audio_buffer.append", "input_audio_buffer.commit"],
        )
        self.assertTrue(socket.messages[-1]["final"])
        self.assertEqual(client._audio_backlog_bytes, 0)
        self.assertFalse(client._pending_final_commit)
        self.assertFalse(client._recovering)
        self.assertIn("stt_recovery_audio_replayed", [event for event, _ in events])
        await client.close()

    async def test_recovery_supervisor_retries_without_raising_to_transport(self) -> None:
        async def ignore_partial(_text, _raw):
            return None

        async def ignore_final(_text):
            return None

        events = []
        client = RSTTClient(
            LabConfig(),
            ignore_partial,
            ignore_final,
            lambda event, data: events.append((event, data)),
        )
        client.recover = AsyncMock(
            side_effect=[
                OSError("gateway unavailable"),
                OSError("gateway restarting"),
                None,
            ]
        )

        with patch("latency_lab.providers.asyncio.sleep", new=AsyncMock()):
            await client._recovery_supervisor("provider_socket_closed")

        self.assertEqual(client.recover.await_count, 3)
        self.assertEqual(
            [event for event, _ in events].count("stt_recovery_wait"), 2
        )
        await client.close()

    async def test_final_rotates_socket_before_delivering_final_to_pipeline(self) -> None:
        events = []
        finals = []

        async def ignore_partial(_text, _raw):
            return None

        async def capture_final(text):
            finals.append(text)
            self.assertTrue(socket.closed)
            self.assertTrue(client._recovering)
            client.ensure_recovery.assert_called()

        class FakeSocket:
            closed = False

            def __init__(self):
                self._messages = [
                    SimpleNamespace(
                        type=aiohttp.WSMsgType.TEXT,
                        data=json.dumps(
                            {"type": "transcription.done", "text": "Goodbye."}
                        ),
                    )
                ]

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self._messages:
                    raise StopAsyncIteration
                return self._messages.pop(0)

            async def close(self):
                self.closed = True

        client = RSTTClient(
            LabConfig(stt_rotate_after_final=True),
            ignore_partial,
            capture_final,
            lambda event, data: events.append((event, data)),
        )
        socket = FakeSocket()
        client.ws = socket
        client.ensure_recovery = Mock()

        await client._receive(socket)

        self.assertEqual(finals, ["Goodbye."])
        self.assertTrue(socket.closed)
        self.assertIsNone(client.ws)
        self.assertEqual(client.ensure_recovery.call_count, 1)
        self.assertIn("stt_turn_socket_rotation", [event for event, _ in events])
        await client.close()

    async def test_provider_error_event_is_contained_and_requests_recovery(self) -> None:
        events = []

        async def ignore_partial(_text, _raw):
            return None

        async def ignore_final(_text):
            return None

        class FakeSocket:
            closed = False

            def __init__(self):
                self._messages = [
                    SimpleNamespace(
                        type=aiohttp.WSMsgType.TEXT,
                        data=json.dumps(
                            {
                                "type": "error",
                                "code": "processing_error",
                                "error": "EngineCore failed",
                            }
                        ),
                    )
                ]

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self._messages:
                    raise StopAsyncIteration
                return self._messages.pop(0)

            async def close(self):
                self.closed = True

        client = RSTTClient(
            LabConfig(),
            ignore_partial,
            ignore_final,
            lambda event, data: events.append((event, data)),
        )
        socket = FakeSocket()
        client.ws = socket
        client.ensure_recovery = Mock()

        await client._receive(socket)

        self.assertIn("stt_error", [event for event, _ in events])
        client.ensure_recovery.assert_called_once_with("provider_socket_closed")
        self.assertTrue(client._recovering)
        await client.close()

    async def test_provider_reason_is_recorded_without_trace_keyword_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            async def send(_message):
                return None

            pipeline = ConversationPipeline(
                LabConfig(trace_dir=root / "traces", report_dir=root / "reports"),
                TraceRecorder(root / "traces", "provider-reason-test"),
                send,
            )

            pipeline._provider_event("stt_recovery_start", {"reason": "final_timeout"})

            event = json.loads(
                pipeline.trace.path.read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["reason"], "provider_protocol_event")
            self.assertEqual(event["data"]["provider_reason"], "final_timeout")
            await pipeline.http.close()

    async def test_pipeline_contains_unexpected_stt_append_failure(self) -> None:
        class FailingSTT:
            def __init__(self):
                self.recovery_reasons = []

            async def append(self, _pcm):
                raise OSError("injected socket failure")

            def ensure_recovery(self, reason):
                self.recovery_reasons.append(reason)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            async def send(_message):
                return None

            pipeline = ConversationPipeline(
                LabConfig(trace_dir=root / "traces", report_dir=root / "reports"),
                TraceRecorder(root / "traces", "contained-append-test"),
                send,
            )
            failing = FailingSTT()
            pipeline.stt = failing

            await pipeline.audio(b"\x00\x00" * 1600)

            self.assertEqual(
                failing.recovery_reasons, ["pipeline_audio_forward_failure"]
            )
            events = [
                json.loads(line)
                for line in pipeline.trace.path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn(
                "stt_audio_forward_error_contained",
                [event["event"] for event in events],
            )
            await pipeline.http.close()

    async def test_final_timeout_recovers_stt_before_returning_idle(self) -> None:
        class FakeSTT:
            def __init__(self):
                self.reasons = []
                self.abandoned = []

            async def recover(self, reason):
                self.reasons.append(reason)

            async def abandon_turn_audio(self, reason):
                self.abandoned.append(reason)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            async def send(_message):
                return None

            pipeline = ConversationPipeline(
                LabConfig(trace_dir=root / "traces", report_dir=root / "reports"),
                TraceRecorder(root / "traces", "recovery-test"),
                send,
            )
            fake_stt = FakeSTT()
            pipeline.stt = fake_stt
            pipeline.state = "awaiting_final"
            pipeline.turn_id = "turn-0001"
            pipeline.turn_number = 1

            with patch("latency_lab.pipeline.asyncio.sleep", new=AsyncMock()):
                await pipeline._final_timeout("turn-0001")

            self.assertEqual(fake_stt.reasons, ["final_timeout"])
            self.assertEqual(fake_stt.abandoned, ["application_final_timeout"])
            self.assertEqual(pipeline.state, "idle")
            self.assertIsNone(pipeline.turn_id)
            events = [
                json.loads(line)
                for line in pipeline.trace.path.read_text(encoding="utf-8").splitlines()
            ]
            reasons = [event["reason"] for event in events if event["event"] == "state_transition"]
            self.assertEqual(
                reasons,
                ["stt_final_timeout_restarting_provider", "stt_recovery_completed"],
            )
            await pipeline.http.close()


class HybridSTTTest(unittest.IsolatedAsyncioTestCase):
    def test_pcm16_wav_wraps_mono_16khz_audio(self) -> None:
        payload = pcm16_wav(b"\x01\x00" * 1600)

        self.assertEqual(payload[:4], b"RIFF")
        self.assertEqual(payload[8:12], b"WAVE")
        self.assertEqual(len(payload), 3244)

    async def test_initial_realtime_failure_keeps_batch_final_available(self) -> None:
        events = []

        async def ignore_partial(_text, _raw):
            return None

        async def ignore_final(_text):
            return None

        client = RSTTClient(
            LabConfig(),
            ignore_partial,
            ignore_final,
            lambda event, data: events.append((event, data)),
        )
        client._open_socket = AsyncMock(side_effect=OSError("gateway unavailable"))

        await client.connect()

        self.assertIn(
            "stt_realtime_connect_degraded", [event for event, _ in events]
        )
        self.assertTrue(events[-1][1]["batch_final_available"])
        await client.close()

    async def test_realtime_done_never_becomes_committed_final(self) -> None:
        events = []
        finals = []

        async def ignore_partial(_text, _raw):
            return None

        async def capture_final(text):
            finals.append(text)

        class FakeSocket:
            closed = False

            def __init__(self):
                self.messages = []
                self._messages = [
                    SimpleNamespace(
                        type=aiohttp.WSMsgType.TEXT,
                        data=json.dumps(
                            {"type": "transcription.done", "text": "Realtime guess"}
                        ),
                    )
                ]

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self._messages:
                    raise StopAsyncIteration
                return self._messages.pop(0)

            async def send_json(self, message):
                self.messages.append(message)

            async def close(self):
                self.closed = True

        client = RSTTClient(
            LabConfig(),
            ignore_partial,
            capture_final,
            lambda event, data: events.append((event, data)),
        )
        socket = FakeSocket()
        client.ws = socket

        await client._receive(socket)

        self.assertEqual(finals, [])
        self.assertIn("stt_realtime_final_ignored", [event for event, _ in events])
        self.assertEqual(socket.messages[-1]["final"], False)
        await client.close()

    async def test_batch_result_is_the_only_authoritative_final(self) -> None:
        events = []
        finals = []

        async def ignore_partial(_text, _raw):
            return None

        async def capture_final(text):
            finals.append(text)

        class FakeResponse:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def raise_for_status(self):
                return None

            async def json(self):
                return {"text": "The committed batch transcript."}

        class FakeSession:
            closed = False

            def post(self, *_args, **_kwargs):
                return FakeResponse()

            async def close(self):
                self.closed = True

        client = RSTTClient(
            LabConfig(),
            ignore_partial,
            capture_final,
            lambda event, data: events.append((event, data)),
        )
        client.session = FakeSession()

        await client._transcribe_batch(b"\x01\x00" * 3200)

        self.assertEqual(finals, ["The committed batch transcript."])
        batch_event = next(data for event, data in events if event == "stt_batch_final")
        self.assertTrue(batch_event["authoritative"])
        await client.close()

    async def test_commit_quiesces_realtime_before_batch_and_resumes_afterward(self) -> None:
        events = []
        order = []
        socket_closed = asyncio.Event()

        async def ignore_partial(_text, _raw):
            return None

        async def ignore_final(_text):
            return None

        class FakeSocket:
            closed = False

            def __init__(self):
                self.messages = []

            async def send_json(self, message):
                self.messages.append(message)

            async def close(self):
                order.append("socket_close")
                self.closed = True
                socket_closed.set()

        client = RSTTClient(
            LabConfig(),
            ignore_partial,
            ignore_final,
            lambda event, data: events.append((event, data)),
        )
        socket = FakeSocket()
        client.ws = socket

        async def receive_until_closed():
            await socket_closed.wait()
            order.append("receiver_finished")

        client.recv_task = asyncio.create_task(receive_until_closed())

        async def transcribe(_audio):
            self.assertTrue(socket.closed)
            self.assertTrue(client.recv_task is None)
            order.append("batch")

        async def reopen():
            order.append("resume")

        client._transcribe_batch = AsyncMock(side_effect=transcribe)
        client._open_socket_locked = AsyncMock(side_effect=reopen)

        await client.commit(b"\x01\x00" * 1600)
        await asyncio.gather(*tuple(client._batch_tasks))

        self.assertEqual(
            order,
            ["socket_close", "receiver_finished", "batch", "resume"],
        )
        self.assertFalse(
            any(message.get("final") is True for message in socket.messages)
        )
        event_names = [event for event, _ in events]
        self.assertLess(
            event_names.index("stt_realtime_quiesced"),
            event_names.index("stt_realtime_resume_start"),
        )
        self.assertIn("stt_realtime_resume_complete", event_names)
        await client.close()

    async def test_recovery_cannot_reopen_realtime_during_batch(self) -> None:
        order = []
        batch_started = asyncio.Event()
        finish_batch = asyncio.Event()

        async def ignore_partial(_text, _raw):
            return None

        async def ignore_final(_text):
            return None

        client = RSTTClient(
            LabConfig(), ignore_partial, ignore_final, lambda _event, _data: None
        )
        client.session = SimpleNamespace(closed=False)

        async def transcribe(_audio):
            order.append("batch_start")
            batch_started.set()
            await finish_batch.wait()
            order.append("batch_end")

        async def reopen():
            order.append("realtime_open")

        client._transcribe_batch = AsyncMock(side_effect=transcribe)
        client._open_socket_locked = AsyncMock(side_effect=reopen)

        batch_task = asyncio.create_task(client._run_authoritative_batch(b"audio"))
        await batch_started.wait()
        recovery_task = asyncio.create_task(client.recover("final_timeout"))
        await asyncio.sleep(0)

        self.assertEqual(order, ["batch_start"])
        self.assertFalse(recovery_task.done())

        finish_batch.set()
        await asyncio.gather(batch_task, recovery_task)

        self.assertEqual(
            order,
            ["batch_start", "batch_end", "realtime_open", "realtime_open"],
        )
        client.session = None
        await client.close()

    async def test_pipeline_commits_only_the_locally_owned_utterance(self) -> None:
        class FakeSTT:
            def __init__(self):
                self.committed = []

            async def append(self, _pcm):
                return None

            async def commit(self, pcm):
                self.committed.append(bytes(pcm))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            async def send(_message):
                return None

            pipeline = ConversationPipeline(
                LabConfig(
                    trace_dir=root / "traces",
                    report_dir=root / "reports",
                    min_speech_ms=200,
                    end_silence_ms=200,
                ),
                TraceRecorder(root / "traces", "hybrid-buffer-test"),
                send,
            )
            fake_stt = FakeSTT()
            pipeline.stt = fake_stt
            voiced = struct.pack("<1600h", *([1000] * 1600))
            silence = b"\x00\x00" * 1600

            await pipeline.audio(voiced)
            await pipeline.audio(voiced)
            await pipeline.audio(silence)
            await pipeline.audio(silence)

            self.assertEqual(len(fake_stt.committed), 1)
            self.assertEqual(fake_stt.committed[0], voiced + voiced + silence + silence)
            if pipeline.final_timeout_task:
                pipeline.final_timeout_task.cancel()
                await asyncio.gather(pipeline.final_timeout_task, return_exceptions=True)
            await pipeline.http.close()


class OpeningGreetingTest(unittest.IsolatedAsyncioTestCase):
    async def test_opening_greeting_does_not_create_a_fake_user_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            messages = []

            async def send(message):
                messages.append(message)

            pipeline = ConversationPipeline(
                LabConfig(trace_dir=root / "traces", report_dir=root / "reports"),
                TraceRecorder(root / "traces", "opening-test"),
                send,
            )

            async def speak(attempt, text, *, phase):
                self.assertEqual(phase, "opening_greeting")
                attempt.answer = text

            pipeline._speak_text = speak
            spoken = await pipeline.speak_opening(
                "Hello, this is Sarah. How may I help you today?"
            )

            self.assertTrue(spoken)
            self.assertEqual(
                pipeline.history,
                [
                    {
                        "role": "assistant",
                        "content": "Hello, this is Sarah. How may I help you today?",
                    }
                ],
            )
            self.assertFalse(any(item["role"] == "user" for item in pipeline.history))
            self.assertEqual(
                [message["type"] for message in messages],
                ["final_assistant_answer", "tts_end"],
            )
            self.assertEqual(pipeline.state, "idle")
            self.assertIsNone(pipeline.turn_id)
            await pipeline.http.close()


class ReportTest(unittest.TestCase):
    def test_report_names_largest_observed_gap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trace = root / "trace.jsonl"
            names = [
                ("user_speech_start", 0),
                ("user_speech_end", 1_000_000_000),
                ("stt_final", 1_600_000_000),
                ("tts_first_audio_chunk", 2_000_000_000),
                ("browser_playback_start", 2_100_000_000),
                ("browser_playback_stop", 3_000_000_000),
                ("turn_close", 3_000_100_000),
            ]
            with trace.open("w", encoding="utf-8") as handle:
                for seq, (event, mono_ns) in enumerate(names, 1):
                    handle.write(
                        json.dumps(
                            {
                                "seq": seq,
                                "session_id": "session-test",
                                "turn_id": "turn-0001",
                                "attempt_id": "attempt-test",
                                "event": event,
                                "mono_ns": mono_ns,
                                "wall_utc": "2026-08-25T00:00:00Z",
                                "state_from": None,
                                "state_to": None,
                                "reason": "test",
                                "data": {},
                            }
                        )
                        + "\n"
                    )
            report = generate_report(trace, root / "reports").read_text(encoding="utf-8")
            self.assertIn("Speech end → audible playback: **1,100.0 ms**", report)
            self.assertIn("largest observed critical-path gap", report)
            self.assertIn("At most one final per turn: **PASS**", report)


class _MemoryTrace:
    def __init__(self) -> None:
        self.events = []

    def record(self, event, **data):
        self.events.append((event, data))


class TurnBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_post_final_partial_cannot_cancel_authorized_generation(self) -> None:
        pipeline = ConversationPipeline.__new__(ConversationPipeline)
        pipeline.turn_id = "turn-0001"
        pipeline.last_closed_turn_id = None
        pipeline.closed_turns = set()
        pipeline.state = "releasing"
        pipeline.trace = _MemoryTrace()
        pipeline.attempt = Attempt(
            id="attempt-final", hypothesis="Hello", speculative=False, authorized=True
        )

        await pipeline.on_partial("language Chinese 嗯。", "language")

        self.assertFalse(pipeline.attempt.cancelled)
        self.assertEqual(pipeline.trace.events, [])

    async def test_short_noise_burst_cannot_cancel_pending_response(self) -> None:
        class FakeSTT:
            async def append(self, _pcm):
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sent = []

            async def send(message):
                sent.append(message)

            pipeline = ConversationPipeline(
                LabConfig(trace_dir=root / "traces", report_dir=root / "reports"),
                TraceRecorder(root / "traces", "noise-test"),
                send,
            )
            pipeline.stt = FakeSTT()
            pipeline.state = "releasing"
            pipeline.turn_id = "turn-0001"
            pipeline.turn_number = 1
            pipeline.attempt = Attempt(
                id="attempt-response", hypothesis="hello", speculative=False, authorized=True
            )
            voiced = struct.pack("<1600h", *([400] * 1600))
            silence = b"\x00\x00" * 1600

            await pipeline.audio(voiced)
            await pipeline.audio(silence)

            self.assertFalse(pipeline.attempt.cancelled)
            self.assertEqual(sent, [])
            self.assertFalse(pipeline.speech_candidate_active)
            await pipeline.http.close()


if __name__ == "__main__":
    unittest.main()
