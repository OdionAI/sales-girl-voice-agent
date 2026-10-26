import unittest
from types import SimpleNamespace

from latency_lab.action_tools import PASSKEY_START_TOOL, passkey_tool_definitions
from latency_lab.passkey import start_requested


class PasskeyStartToolTests(unittest.TestCase):
    def test_not_advertised_when_absent(self):
        self.assertEqual(passkey_tool_definitions(SimpleNamespace(tools=[])), [])

    def test_not_advertised_when_inactive(self):
        ctx = SimpleNamespace(tools=[{"name": PASSKEY_START_TOOL, "is_active": False}])
        self.assertEqual(passkey_tool_definitions(ctx), [])

    def test_advertised_when_configured(self):
        ctx = SimpleNamespace(tools=[{
            "name": PASSKEY_START_TOOL, "is_active": True,
            "url": "internal://wema-start-passkey",
        }])
        definitions = passkey_tool_definitions(ctx)
        self.assertEqual(len(definitions), 1)
        function = definitions[0]["function"]
        self.assertEqual(function["name"], PASSKEY_START_TOOL)
        self.assertEqual(function["parameters"]["properties"], {})

    def test_pipeline_imports_the_tool_constant(self):
        import latency_lab.pipeline as pipeline
        self.assertEqual(pipeline.PASSKEY_START_TOOL, PASSKEY_START_TOOL)


class StartRequestedTests(unittest.TestCase):
    """The isolation agent must be started by the worker on a clear agreement,
    not left to the model's (unreliable) tool choice."""

    def test_clear_agreements_match(self):
        for text in ("Can you start?", "yes", "Yes please", "go ahead", "sure",
                     "Sure, let's do it", "okay", "run the passkey test",
                     "please start the passkey check", "alright do it"):
            self.assertTrue(start_requested(text), text)

    def test_refusals_and_other_topics_do_not_match(self):
        for text in ("", "no", "not now", "wait", "hold on", "stop", "cancel that",
                     "what is my balance?", "who is this?", "later"):
            self.assertFalse(start_requested(text), text)


if __name__ == "__main__":
    unittest.main()
