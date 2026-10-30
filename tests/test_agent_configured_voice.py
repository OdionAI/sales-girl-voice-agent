from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

from latency_lab.config import (
    agent_voice_allowlist,
    apply_agent_configured_voice,
    lab_config_from_environ,
)


class AgentConfiguredVoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.pop("RVC_LAB_AGENT_VOICES", None)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._saved is None:
            os.environ.pop("RVC_LAB_AGENT_VOICES", None)
        else:
            os.environ["RVC_LAB_AGENT_VOICES"] = self._saved

    def test_agent_configured_voice_used_as_baseline(self) -> None:
        config = lab_config_from_environ()
        updated = apply_agent_configured_voice(
            config, SimpleNamespace(tts_voice_id="access-ghana-cindy"), {}
        )
        self.assertEqual(updated.tts_voice, "access-ghana-cindy")

    def test_session_override_wins_over_agent_voice(self) -> None:
        config = lab_config_from_environ()
        updated = apply_agent_configured_voice(
            config,
            SimpleNamespace(tts_voice_id="access-ghana-cindy"),
            {"tts_voice_id": "wema-en-ng-female"},
        )
        # The per-session choice is applied by apply_runtime_overrides; this
        # helper must leave it untouched rather than injecting the agent voice.
        self.assertEqual(updated.tts_voice, config.tts_voice)

    def test_unenrolled_agent_voice_is_ignored(self) -> None:
        config = lab_config_from_environ()
        updated = apply_agent_configured_voice(
            config, SimpleNamespace(tts_voice_id="d270a5cec6914373b9deed1d1c3cbade"), {}
        )
        self.assertEqual(updated.tts_voice, config.tts_voice)

    def test_missing_agent_voice_keeps_default(self) -> None:
        config = lab_config_from_environ()
        updated = apply_agent_configured_voice(
            config, SimpleNamespace(tts_voice_id=""), {}
        )
        self.assertEqual(updated.tts_voice, config.tts_voice)

    def test_none_overrides_is_tolerated(self) -> None:
        config = lab_config_from_environ()
        updated = apply_agent_configured_voice(
            config, SimpleNamespace(tts_voice_id="access-ghana-cindy"), None
        )
        self.assertEqual(updated.tts_voice, "access-ghana-cindy")

    def test_allowlist_is_env_extensible_and_case_insensitive(self) -> None:
        os.environ["RVC_LAB_AGENT_VOICES"] = "my-new-voice,access-ghana-cindy"
        self.assertIn("my-new-voice", agent_voice_allowlist())
        config = lab_config_from_environ()
        updated = apply_agent_configured_voice(
            config, SimpleNamespace(tts_voice_id="My-New-Voice"), {}
        )
        self.assertEqual(updated.tts_voice, "My-New-Voice")


if __name__ == "__main__":
    unittest.main()
