from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import main
from agent.odion_tts import (
    DEFAULT_ODION_TTS_BASE_URL,
    DEFAULT_ODION_TTS_STREAM_PATH,
    OdionTTS,
)


class OdionTTSTests(unittest.TestCase):
    def test_endpoint_appends_stream_path_for_host_only_base_url(self) -> None:
        engine = OdionTTS(
            owner_id="owner-123",
            voice_id=None,
            base_url=DEFAULT_ODION_TTS_BASE_URL,
        )

        self.assertEqual(
            engine._opts.endpoint_url,
            f"{DEFAULT_ODION_TTS_BASE_URL}{DEFAULT_ODION_TTS_STREAM_PATH}",
        )

    def test_endpoint_uses_full_stream_endpoint_url_exactly(self) -> None:
        engine = OdionTTS(
            owner_id="owner-123",
            voice_id=None,
            base_url="http://34.122.84.20/api/v1/tts/stream",
        )

        self.assertEqual(
            engine._opts.endpoint_url,
            "http://34.122.84.20/api/v1/tts/stream",
        )

    def test_participant_metadata_preserves_runtime_overrides(self) -> None:
        metadata = json.dumps(
            {
                "runtime_overrides": {
                    "stt_provider": "odion_stt",
                    "stt_base_url": "http://34.122.84.20/stt/v1/stt",
                    "tts_base_url": "http://34.122.84.20/api/v1/tts/stream",
                    "ignored": "not copied",
                }
            }
        )
        ctx = SimpleNamespace(
            room=SimpleNamespace(
                remote_participants={
                    "user": SimpleNamespace(metadata=metadata),
                }
            )
        )

        overrides = main._extract_tts_overrides_from_ctx(ctx)

        self.assertEqual(
            overrides["runtime_overrides"],
            {
                "stt_provider": "odion_stt",
                "stt_base_url": "http://34.122.84.20/stt/v1/stt",
                "tts_base_url": "http://34.122.84.20/api/v1/tts/stream",
            },
        )

    def test_tts_builder_uses_runtime_tts_base_url_without_top_level_endpoint(self) -> None:
        with (
            patch("main.deepgram.TTS", return_value=object()),
            patch.object(main, "ENABLE_ODION_TTS_EN", True),
            patch.object(main, "ODION_TTS_EXPERIMENT_OWNER_ID", ""),
            patch.object(main, "ODION_TTS_EXPERIMENT_VOICE_ID", ""),
        ):
            engine = main._build_tts_engine_for_language(
                language="en",
                active_agent_config={},
                userdata={
                    "runtime_overrides": {
                        "tts_base_url": "http://34.122.84.20/api/v1/tts/stream",
                    }
                },
                business_id="business-123",
            )

        self.assertIsInstance(engine, OdionTTS)
        self.assertEqual(
            engine._opts.endpoint_url,
            "http://34.122.84.20/api/v1/tts/stream",
        )


if __name__ == "__main__":
    unittest.main()
