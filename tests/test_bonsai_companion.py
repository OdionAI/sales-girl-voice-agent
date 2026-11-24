import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "deployment/lagos/bonsai_companion.py"
spec = importlib.util.spec_from_file_location("bonsai_companion", PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CompanionTests(unittest.TestCase):
    def test_exclusive_scope_and_shared_default_are_preserved(self):
        env = module.environment(["RVC_LAB_LLM_PROVIDER=deepseek", "RVC_AICC_SIP_ROUTE=fixture", "TOKEN=private"], "local-test")
        self.assertEqual(env["RVC_LAB_LLM_PROVIDER"], "deepseek")
        self.assertEqual(env["RVC_LAB_TRIAL_LLM_EXCLUSIVE"], "true")
        self.assertEqual(env["RVC_LAB_TRIAL_LLM_BUSINESS_ID"], module.BUSINESS)
        self.assertEqual(env["RVC_LAB_TRIAL_LLM_AGENT_IDS"].split(","), list(module.AGENTS))
        self.assertEqual(env["RVC_LAB_LIVEKIT_AGENT_NAME"], "rvc-bonsai-demo")
        self.assertEqual(env["RVC_LAB_LIVEKIT_WORKER_PORT"], "18084")
        self.assertEqual(env["RVC_LAB_MAX_JOBS"], "1")
        self.assertNotIn("RVC_AICC_SIP_ROUTE", env)
        self.assertEqual(env["RVC_LAB_TRIAL_LLM_FIRST_OUTPUT_TIMEOUT_SECONDS"], "90")
        self.assertEqual(env["LIVEKIT_RECORDING_ENABLED"], "false")

    def test_invalid_or_partial_secrets_and_multiline_env_fail_closed(self):
        for key in ("", "secret\nextra"):
            with self.assertRaises(ValueError):
                module.environment([], key)
        with self.assertRaises(ValueError):
            module.environment(["BROKEN=one\ntwo"], "local-test")

    def test_plan_does_not_create_or_restart_any_container(self):
        calls = []
        source = [{"State": {"Running": True}, "HostConfig": {"NetworkMode": "host"}, "Config": {"Image": "live-image", "Env": []}}]
        def fake_run(*args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(source))
        with patch.object(sys, "argv", ["script", "--image", "attentive/voice:bonsai-ci-test"]), patch.dict(os.environ, {"BONSAI_TRIAL_API_KEY": "fixture"}), patch.object(module, "run", side_effect=fake_run), patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)), contextlib.redirect_stdout(io.StringIO()) as output:
            module.main()
        self.assertEqual(calls, [("docker", "inspect", module.SOURCE)])
        self.assertNotIn("fixture", output.getvalue())

    def test_apply_requires_durable_tunnel_before_container_creation(self):
        calls = []
        source = [{"State": {"Running": True}, "HostConfig": {"NetworkMode": "host"}, "Config": {"Image": "live-image", "Env": []}}]
        def fake_run(*args, **kwargs):
            calls.append(args)
            if args[:2] == ("systemctl", "is-active"):
                raise subprocess.CalledProcessError(3, args)
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(source))
        with patch.object(sys, "argv", ["script", "--image", "attentive/voice:bonsai-ci-test", "--apply"]), patch.dict(os.environ, {"BONSAI_TRIAL_API_KEY": "fixture"}), patch.object(module, "run", side_effect=fake_run), patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(subprocess.CalledProcessError):
                module.main()
        self.assertNotIn("run", [args[1] for args in calls if args[0] == "docker"])


if __name__ == "__main__":
    unittest.main()
