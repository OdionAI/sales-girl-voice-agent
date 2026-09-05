from __future__ import annotations

import unittest

import main
from prompts.wema import with_wema_tool_requirements


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
