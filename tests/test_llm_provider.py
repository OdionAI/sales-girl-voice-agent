import os
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

    def test_build_llm_uses_environment_wide_model_selection(self) -> None:
        with (
            patch("main.groq.LLM", side_effect=lambda **kwargs: object()),
            patch.object(main, "FallbackGroqLLM") as factory,
        ):
            main._build_llm_for_language(
                language="en",
                userdata={
                    "platform_voice_defaults": {
                        "llm": {"provider": "groq", "model": "llama-3.3-70b-versatile"}
                    }
                },
            )
        factory.assert_called_once_with(
            primary_model="llama-3.3-70b-versatile", backup_model=""
        )

    def test_build_llm_uses_environment_wide_openai_compatible_endpoint(self) -> None:
        with patch("main.openai.LLM", return_value=object()) as llm_factory:
            main._build_llm_for_language(
                language="en",
                userdata={
                    "platform_voice_defaults": {
                        "llm": {
                            "provider": "openai_compatible",
                            "model": "custom-chat-model",
                            "base_url": "https://ai-server.example.test/v1/chat/completions",
                        }
                    }
                },
            )
        self.assertEqual(llm_factory.call_args.kwargs["model"], "custom-chat-model")
        self.assertEqual(llm_factory.call_args.kwargs["base_url"], "https://ai-server.example.test/v1")
        self.assertEqual(llm_factory.call_args.kwargs["api_key"], "EMPTY")

    def test_build_llm_uses_environment_wide_huawei_maas_selection(self) -> None:
        with (
            patch.dict("os.environ", {"MAAS_API_KEY": "test-maas-key"}),
            patch("main.openai.LLM", return_value=object()) as llm_factory,
        ):
            main._build_llm_for_language(
                language="en",
                userdata={
                    "platform_voice_defaults": {
                        "llm": {
                            "provider": "maas",
                            "model": "glm-5.2",
                            "base_url": "https://maas.example.test/openai/v1",
                        }
                    }
                },
            )
        self.assertEqual(llm_factory.call_args.kwargs["model"], "glm-5.2")
        self.assertEqual(llm_factory.call_args.kwargs["base_url"], "https://maas.example.test/openai/v1")
        self.assertEqual(llm_factory.call_args.kwargs["api_key"], "test-maas-key")

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
                    "enable_thinking": False,
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

    def test_qwen_runtime_override_can_enable_thinking(self) -> None:
        with (
            patch.dict(os.environ, {"QWEN_LLM_DISABLE_THINKING": "true"}),
            patch("main.openai.LLM", return_value=object()) as llm_factory,
        ):
            main._build_llm_for_language(
                language="en",
                userdata={
                    "runtime_overrides": {
                        "llm_provider": "qwen_openai",
                        "llm_base_url": "http://npu.test/v1/chat/completions",
                        "llm_disable_thinking": "false",
                    }
                },
            )

        self.assertTrue(
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
