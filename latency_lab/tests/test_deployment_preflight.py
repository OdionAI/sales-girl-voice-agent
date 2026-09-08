import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from latency_lab.handoff.verify_deployment import config_errors, trace_errors

MANIFEST = json.loads((Path(__file__).parents[1] / "handoff/runtime-manifest.json").read_text())


class DeploymentPreflightTests(unittest.TestCase):
    def test_captured_configuration_matches(self):
        expected = MANIFEST["expected_config"]
        self.assertEqual(config_errors(SimpleNamespace(**expected), expected), [])

    def test_thinking_and_model_drift_fail_without_disclosing_values(self):
        expected = MANIFEST["expected_config"]
        config = SimpleNamespace(**expected)
        config.llm_enable_thinking = True
        config.stt_ws_url = "ws://private-credential@example.invalid"
        errors = config_errors(config, expected)
        self.assertEqual(len(errors), 2)
        self.assertNotIn("private-credential", str(errors))

    def test_missing_trace_is_not_success(self):
        self.assertTrue(trace_errors([], MANIFEST))

    def test_trace_requires_hybrid_and_attached_tools(self):
        opened = {
            "transport": "livekit", "agent_config_loaded": True,
            "configured_agent_name": "SAW", "configured_tool_count": 8,
            "supported_action_tool_count": 8, "stt_final_authority": "whisper_realtime",
            "stt_batch_url": "", "initial_codec_chunk_frames": 4,
            "end_silence_ms": 300, "memory_compaction_enabled": True,
            "memory_context_tokens_estimate": 6000,
        }
        events = [{"event": "session_open", "data": opened},
                  {"event": "livekit_job_received", "data": {
                      "agent_name": "rvc-livekit-comparison", "room_name": "rvc-livekit-test"}}]
        self.assertEqual(trace_errors(events, MANIFEST), [])
        opened["configured_tool_count"] = 0
        self.assertTrue(trace_errors(events, MANIFEST))


if __name__ == "__main__":
    unittest.main()
