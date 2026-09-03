from __future__ import annotations

import unittest

import main


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


if __name__ == "__main__":
    unittest.main()
