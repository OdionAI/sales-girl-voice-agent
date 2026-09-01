import unittest
from unittest.mock import Mock, patch

import main


class LlmProviderTests(unittest.TestCase):
    def test_build_llm_defaults_to_google(self) -> None:
        with (
            patch.object(main, "LLM_PROVIDER", "google"),
            patch("main.google.LLM", side_effect=lambda **kwargs: object()),
        ):
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

    def test_build_llm_uses_full_qwen_endpoint_override_without_fallback(self) -> None:
        runtime_overrides = {
            "llm_provider": "qwen_openai",
            "llm_model": "qwen3.8_27b",
            "llm_base_url": (
                "http://102.88.137.124:8080/"
                "qwen38-standard/v1/chat/completions"
            ),
        }

        with patch("main.openai.LLM", return_value=object()) as llm_factory:
            main._build_llm_for_language(
                language="en",
                userdata={"runtime_overrides": runtime_overrides},
            )

        llm_factory.assert_called_once_with(
            model="qwen3.8_27b",
            api_key="EMPTY",
            base_url="http://102.88.137.124:8080/qwen38-standard/v1",
            temperature=0,
            extra_body={
                "chat_template_kwargs": {
                    "enable_thinking": True,
                }
            },
        )

    def test_qwen_runtime_override_can_disable_thinking(self) -> None:
        with patch("main.openai.LLM", return_value=object()) as llm_factory:
            main._build_llm_for_language(
                language="en",
                userdata={
                    "runtime_overrides": {
                        "llm_provider": "qwen_openai",
                        "llm_base_url": "http://npu.test/v1/chat/completions",
                        "llm_disable_thinking": "true",
                    }
                },
            )

        self.assertFalse(
            llm_factory.call_args.kwargs["extra_body"]
            ["chat_template_kwargs"]["enable_thinking"]
        )

    def test_first_turn_uses_user_input_for_openai_compatible_models(self) -> None:
        session = Mock()

        main._trigger_first_turn(
            session,
            language="en",
            business_use_case="",
        )

        session.generate_reply.assert_called_once_with(
            user_input=main._kickoff_prompt_for_language("en", ""),
            input_modality="text",
        )

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
