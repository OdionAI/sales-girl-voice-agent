import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / ".github" / "scripts" / "detect_ci_scope.py"
SPEC = importlib.util.spec_from_file_location("detect_ci_scope", MODULE_PATH)
assert SPEC and SPEC.loader
detect_ci_scope = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(detect_ci_scope)


class CICheckScopeTests(unittest.TestCase):
    def test_python_voice_change_skips_native_build(self):
        self.assertEqual(
            detect_ci_scope.classify(["agent/voice_auth.py"]),
            {"python": True, "passkey": True, "native": False},
        )

    def test_rust_change_runs_only_native_checks(self):
        self.assertEqual(
            detect_ci_scope.classify(["rust/src/coordinator.rs"]),
            {"python": False, "passkey": False, "native": True},
        )

    def test_docs_and_deploy_workflow_changes_do_not_rebuild_app(self):
        self.assertEqual(
            detect_ci_scope.classify(
                ["deployment/lagos/README.md", ".github/workflows/deploy-lagos.yml"]
            ),
            {"python": False, "passkey": False, "native": False},
        )

    def test_ci_or_scope_detector_change_runs_complete_matrix(self):
        self.assertEqual(
            detect_ci_scope.classify([".github/workflows/ci.yml"]),
            {"python": True, "passkey": True, "native": True},
        )


if __name__ == "__main__":
    unittest.main()
