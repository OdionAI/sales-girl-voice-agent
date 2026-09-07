from __future__ import annotations

import unittest
from unittest.mock import patch

import main
from prompts.wema import with_wema_tool_requirements


class WemaBusinessRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_wema_tools_preserve_saved_identity_and_tool_guidance(self) -> None:
        saved = (
            "You are SAW, Wema Bank's voice banking assistant. Check account balance "
            "and recent transactions. Fidelity Bank is a possible transfer destination."
        )
        config = {
            "name": "SAW", "instructions": saved,
            "tools": [{"name": "wema_get_balance", "description": "Check the balance."}],
        }
        userdata = {"end_user_id": "caller@example.com", "business_id": "wema-business"}
        with patch.object(main, "FIDELITY_BUSINESS_IDS", set()), \
                patch.object(main, "EKEDC_BUSINESS_IDS", set()), \
                patch.object(main, "DEFAULT_BUSINESS_USE_CASE", "fidelity"):
            use_case = main._detect_business_use_case(active_agent_config=config, userdata=userdata)
        self.assertEqual(use_case, "generic")
        userdata["business_use_case"] = use_case
        main._hydrate_userdata_from_active_agent_config(userdata, config, use_case)
        base = main._effective_base_prompt(
            static_prompt="Default", active_agent_config=config,
            business_use_case=use_case, language="en",
        )
        with patch.object(main, "conversation_service_enabled", return_value=False), \
                patch.object(main, "CONVERSATION_SERVICE_REQUIRED", False), \
                patch.object(main, "_instructions_with_resume_context", side_effect=lambda text, _: text):
            instructions = await main._instructions_with_context(base, userdata)
        self.assertTrue(instructions.startswith(saved))
        self.assertIn("wema_get_balance is enabled", instructions)
        self.assertNotIn("You are Fidelity Bank's customer care assistant", instructions)
        with patch.object(main, "ops_get_account_overview") as overview:
            self.assertEqual(await main._build_preloaded_ops_context(userdata), "")
        overview.assert_not_called()

    def test_legacy_fidelity_tool_routing_is_unchanged(self) -> None:
        self.assertEqual(main._detect_business_use_case(
            active_agent_config={"tools": [{"name": "account_overview"}]}, userdata={},
        ), "fidelity")

    def test_explicit_business_routing_still_has_precedence(self) -> None:
        business_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        with patch.object(main, "FIDELITY_BUSINESS_IDS", {business_id}):
            self.assertEqual(main._detect_business_use_case(
                active_agent_config={"tools": [{"name": "wema_get_balance"}]},
                userdata={"business_id": business_id},
            ), "fidelity")


class SpokenStyleCompositionTests(unittest.TestCase):
    def test_english_prompt_receives_nigerian_spoken_style(self) -> None:
        prompt = main._instructions_with_spoken_style(
            "You are the Wema banking assistant.", "en"
        )

        self.assertIn("You are the Wema banking assistant.", prompt)
        self.assertIn("Natural Nigerian spoken style:", prompt)
        self.assertIn('such as "No wahala"', prompt)
        self.assertIn('literal spoken hesitation such as "uh", "um", or "hmm"', prompt)
        self.assertIn("at least one of the first three ordinary", prompt)
        self.assertIn('Never output the plural labels "uhs" or "ums"', prompt)
        self.assertIn("Never read headings, markdown, bullets", prompt)

    def test_non_english_prompt_is_unchanged(self) -> None:
        prompt = "Vous etes le conseiller bancaire."

        self.assertEqual(
            main._instructions_with_spoken_style(prompt, "fr"),
            prompt,
        )

    def test_spoken_style_is_not_added_twice(self) -> None:
        once = main._instructions_with_spoken_style("Base prompt", "en")

        self.assertEqual(
            main._instructions_with_spoken_style(once, "en"),
            once,
        )


class WemaToolGroundingTests(unittest.TestCase):
    def test_non_wema_agent_is_unchanged(self) -> None:
        for names in ([], ["create_ticket", "search_business_knowledge"]):
            with self.subTest(names=names):
                self.assertEqual(with_wema_tool_requirements("Original prompt", names), "Original prompt")

    def test_lookup_requirements_follow_existing_context_and_style(self) -> None:
        original = main._instructions_with_spoken_style("Saved Wema instructions", "en")
        prompt = with_wema_tool_requirements(original, ["wema_get_balance", "wema_get_transactions"])

        self.assertTrue(prompt.startswith(original.rstrip()))
        self.assertIn("requires wema_get_balance in the same turn", prompt)
        self.assertIn("requires wema_get_transactions before", prompt)
        self.assertIn("Actually invoke the function", prompt)
        self.assertIn("Never speak placeholders", prompt)
        self.assertIn("never simulate authorization or bypass a blocked result", prompt)
        self.assertIn("If a result is blocked, failed or needs input", prompt)
        self.assertIn("Speaking-style examples are not responses to copy", prompt)

    def test_only_enabled_lookups_are_named(self) -> None:
        for name, absent in (("wema_get_balance", "wema_get_transactions"),
                             ("wema_get_transactions", "wema_get_balance")):
            with self.subTest(name=name):
                prompt = with_wema_tool_requirements("Original prompt", [name])
                self.assertIn(name, prompt)
                self.assertNotIn(absent, prompt)

    def test_other_wema_tools_get_grounding_without_adding_lookup_capabilities(self) -> None:
        prompt = with_wema_tool_requirements("Original prompt", ["wema_prepare_transfer"])
        self.assertIn("Actually invoke the function", prompt)
        self.assertNotIn("wema_get_balance", prompt)
        self.assertNotIn("wema_get_transactions", prompt)


if __name__ == "__main__":
    unittest.main()
