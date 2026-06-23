import unittest
from unittest.mock import patch

import main


class LlmProviderTests(unittest.TestCase):
    def test_build_llm_defaults_to_google(self) -> None:
        with patch.object(main, "LLM_PROVIDER", "google"):
            llm_engine = main._build_llm_for_language(language="en")
            self.assertIsInstance(llm_engine, main.FallbackGoogleLLM)
            self.assertEqual(llm_engine.provider, "google")

    def test_build_llm_uses_groq_when_configured(self) -> None:
        with (
            patch.object(main, "LLM_PROVIDER", "groq"),
            patch("main.groq.LLM", side_effect=lambda **kwargs: object()),
        ):
            llm_engine = main._build_llm_for_language(language="en")
            self.assertIsInstance(llm_engine, main.FallbackGroqLLM)
            self.assertEqual(llm_engine.provider, "groq")
            self.assertEqual(llm_engine.model, main.GROQ_LLM_MODEL_EN)

    def test_groq_qwen_models_disable_reasoning_for_voice_latency(self) -> None:
        kwargs = main._groq_llm_kwargs_for_model("qwen/qwen3-32b")
        self.assertEqual(kwargs, {"reasoning_effort": "none"})

    def test_groq_compound_models_use_default_kwargs(self) -> None:
        kwargs = main._groq_llm_kwargs_for_model("groq/compound")
        self.assertEqual(kwargs, {})

    def test_groq_non_qwen_models_use_default_kwargs(self) -> None:
        kwargs = main._groq_llm_kwargs_for_model("llama-3.1-8b-instant")
        self.assertEqual(kwargs, {})


if __name__ == "__main__":
    unittest.main()
