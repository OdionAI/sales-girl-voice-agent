import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import aiohttp
from aiohttp import web

from latency_lab.config import LabConfig
from latency_lab.pidgin import (
    DYNAMIC_PIDGIN_CHECKPOINT, DYNAMIC_PIDGIN_MODEL, DYNAMIC_PIDGIN_URL, EJIRO_MODEL, EJIRO_URL,
    SBPN_MODEL, EjiroTTSClient, apply_pidgin_stack, split_text,
)


class DynamicPidginRoutingTests(unittest.TestCase):
    def test_saved_voices_select_new_endpoint_without_changing_llm_or_business_context(self):
        base = LabConfig(llm_model="unchanged", llm_api_key="test-only")
        for voice in ("Maro", "rukky"):
            context = SimpleNamespace(tts_language_hint="Nigerian Pidgin", tts_voice_id=voice)
            result = apply_pidgin_stack(base, context)
            self.assertEqual(result.tts_voice, voice.lower())
            self.assertEqual(result.tts_model, DYNAMIC_PIDGIN_MODEL)
            self.assertEqual(result.tts_url, DYNAMIC_PIDGIN_URL)
            self.assertEqual(result.tts_seed, "0")
            self.assertEqual(result.stt_model, SBPN_MODEL)
            self.assertEqual(context.tts_voice_id, voice)
            for key in vars(base):
                if key.startswith("llm_") or key.startswith("engagement_") or key.startswith("turn_"):
                    self.assertEqual(getattr(result, key), getattr(base, key), key)

    def test_explicit_voice_wins_and_leaving_pidgin_does_not_leak_a_voice(self):
        context = SimpleNamespace(tts_language_hint="pcm", tts_voice_id="maro")
        selected = apply_pidgin_stack(LabConfig(), context,
            {"runtime_overrides": {"tts_voice_id": "Rukky"}})
        self.assertEqual(selected.tts_voice, "rukky")
        base = LabConfig()
        self.assertIs(apply_pidgin_stack(base, context,
            {"runtime_overrides": {"tts_language_hint": "en"}}), base)

    def test_favour_and_saved_legacy_aliases_remain_unchanged(self):
        for voice in ("", "default", "Favour", "ejiro", "edro", EJIRO_MODEL, "mms-pcm-ejiro-3h"):
            result = apply_pidgin_stack(LabConfig(), SimpleNamespace(tts_language_hint="pcm", tts_voice_id=voice))
            self.assertEqual((result.tts_voice, result.tts_model, result.tts_url, result.tts_seed),
                             ("favour", EJIRO_MODEL, EJIRO_URL, "17"))

    def test_existing_unrecognized_saved_ids_keep_their_legacy_route(self):
        result = apply_pidgin_stack(LabConfig(),
            SimpleNamespace(tts_language_hint="pcm", tts_voice_id="existing-legacy-id"))
        self.assertEqual((result.tts_model, result.tts_voice, result.tts_url),
                         (EJIRO_MODEL, "favour", EJIRO_URL))


class DynamicPidginStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.voice = "maro"
        self.status = 200
        self.audio = b"\x01\x00" * 4800
        self.headers = {"Content-Type": "audio/pcm", "X-Audio-Sample-Rate": "24000",
                        "X-Audio-Channels": "1", "X-Model": DYNAMIC_PIDGIN_MODEL,
                        "X-Voice": self.voice, "X-Speaker-ID": self.voice,
                        "X-Checkpoint-SHA256": DYNAMIC_PIDGIN_CHECKPOINT,
                        "X-Voice-Profile": self.voice + "-flash-v1", "X-Seed": "0", "X-Speed": "1"}
        self.delay = False
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

        async def tts(request):
            self.requests.append(await request.json())
            if self.delay:
                response = web.StreamResponse(status=self.status, headers=self.headers)
                await response.prepare(request)
                await response.write(self.audio[:3199])
                await self.release.wait()
                try:
                    await response.write(self.audio[3199:])
                    await response.write_eof()
                except (ConnectionResetError, aiohttp.ClientConnectionError):
                    pass
                self.finished.set()
                return response
            return web.Response(status=self.status, body=self.audio, headers=self.headers)

        app = web.Application()
        app.router.add_post("/tts", tts)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/tts"
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self):
        self.release.set()
        await self.session.close()
        await self.runner.cleanup()

    def client(self):
        config = apply_pidgin_stack(LabConfig(),
            SimpleNamespace(tts_language_hint="pcm", tts_voice_id=self.voice))
        return EjiroTTSClient(replace(config, tts_url=self.url), self.session)

    async def test_each_voice_is_explicit_and_pcm_is_not_resampled(self):
        text = "Abeg check account 0012345678."
        for voice in ("maro", "rukky"):
            self.voice = voice
            self.headers["X-Voice"] = voice
            self.headers["X-Speaker-ID"] = voice
            self.headers["X-Voice-Profile"] = voice + "-flash-v1"
            client = self.client()
            client.usage = SimpleNamespace(tts_accepted=Mock())
            with patch("latency_lab.pidgin.rtc.AudioResampler", side_effect=AssertionError("Do not resample")):
                rate, chunks = await client.stream(text)
                self.assertEqual(b"".join([chunk async for chunk in chunks]), self.audio)
            self.assertEqual(rate, 24000)
            self.assertEqual(self.requests[-1], {
                "model": DYNAMIC_PIDGIN_MODEL, "voice": voice, "language": "pcm",
                "input": text, "response_format": "pcm", "stream": True,
                "stream_format": "audio", "seed": 0, "speed": 1})
            client.usage.tts_accepted.assert_called_once_with(text)

    async def test_yields_before_eof_and_preserves_odd_network_boundaries(self):
        self.delay = True
        _, chunks = await self.client().stream("How you dey?")
        first = await asyncio.wait_for(anext(chunks), 2)
        self.assertFalse(self.finished.is_set())
        self.assertEqual(len(first) % 2, 0)
        self.release.set()
        self.assertEqual(first + b"".join([chunk async for chunk in chunks]), self.audio)

    async def test_long_text_is_lossless_and_voice_never_changes_between_parts(self):
        text = "Abeg give me small time make I check am for you. " * 9
        _, chunks = await self.client().stream(text)
        audio = b"".join([chunk async for chunk in chunks])
        self.assertEqual("".join(item["input"] for item in self.requests), text)
        self.assertEqual([item["input"] for item in self.requests], split_text(text, 140))
        self.assertEqual({item["voice"] for item in self.requests}, {"maro"})
        self.assertEqual(len(audio), len(self.audio) * len(self.requests))

    async def test_cancellation_does_not_start_the_next_part(self):
        self.delay = True
        _, chunks = await self.client().stream("Abeg check am. " * 60)
        await asyncio.wait_for(anext(chunks), 2)
        await chunks.aclose()
        self.release.set()
        self.assertEqual(len(self.requests), 1)

    async def test_mismatched_voice_model_or_rate_rejected_before_audio(self):
        for key, value in (("X-Voice", "favour"), ("X-Model", "other-model"),
                           ("X-Audio-Sample-Rate", "16000"), ("X-Audio-Channels", "2"),
                           ("X-Speaker-ID", "rukky"), ("X-Checkpoint-SHA256", "wrong-checkpoint"),
                           ("X-Seed", "42"), ("X-Speed", "2"), ("X-Voice-Profile", "other-profile")):
            original = self.headers[key]
            self.headers[key] = value
            _, chunks = await self.client().stream("Abeg check am.")
            with self.assertRaisesRegex(ValueError, "unexpected voice, model or PCM format"):
                await anext(chunks)
            self.headers[key] = original

    async def test_empty_or_truncated_audio_and_overload_do_not_retry_or_fallback(self):
        for body in (b"", b"\x01"):
            self.audio = body
            _, chunks = await self.client().stream("Abeg check am.")
            with self.assertRaisesRegex(ValueError, "empty or truncated"):
                _ = [chunk async for chunk in chunks]
        self.requests.clear()
        self.status = 429
        _, chunks = await self.client().stream("Abeg check am.")
        with self.assertRaises(aiohttp.ClientResponseError):
            await anext(chunks)
        self.assertEqual(len(self.requests), 1)


if __name__ == "__main__":
    unittest.main()
