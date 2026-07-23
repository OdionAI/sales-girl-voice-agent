import unittest
from unittest.mock import patch

import main


class _FakeSession:
    def __init__(self) -> None:
        self.generate_reply_kwargs = None

    def generate_reply(self, **kwargs) -> None:
        self.generate_reply_kwargs = kwargs


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

    def test_groq_qwen_models_disable_reasoning_for_voice_latency(self) -> None:
        kwargs = main._groq_llm_kwargs_for_model("qwen/qwen3-32b")
        self.assertEqual(kwargs, {"reasoning_effort": "none"})

    def test_groq_compound_models_use_default_kwargs(self) -> None:
        kwargs = main._groq_llm_kwargs_for_model("groq/compound")
        self.assertEqual(kwargs, {})

    def test_groq_non_qwen_models_use_default_kwargs(self) -> None:
        kwargs = main._groq_llm_kwargs_for_model("llama-3.1-8b-instant")
        self.assertEqual(kwargs, {})

    def test_first_turn_gives_maas_a_synthetic_user_message(self) -> None:
        session = _FakeSession()

        main._trigger_first_turn(
            session,
            language="fr",
            business_use_case="generic",
            configured_agent_name="Sonia",
        )

        kwargs = session.generate_reply_kwargs
        self.assertIsNotNone(kwargs)
        messages = kwargs["chat_ctx"].messages()
        self.assertEqual([message.role for message in messages], ["user"])
        self.assertIn("vient de se connecter", messages[0].text_content)
        self.assertEqual(messages[0].extra, {"synthetic_kickoff": True})
        self.assertNotIn("user_input", kwargs)
        self.assertIn("Saluez l'appelant en français", kwargs["instructions"])
        self.assertIn("Votre nom est exactement « Sonia »", kwargs["instructions"])

    def test_generic_prompt_enforces_plain_concise_voice_output(self) -> None:
        prompt = main._effective_base_prompt(
            static_prompt="fallback",
            active_agent_config={
                "instructions": "Vous êtes Sonia, agente du service consulaire.",
                "tools": [],
            },
            business_use_case="generic",
            language="fr",
        )

        self.assertIn("Never output Markdown headings", prompt)
        self.assertIn("two to four concise sentences", prompt)
        self.assertIn("never invent or switch to another personal name", prompt)


if __name__ == "__main__":
    unittest.main()
