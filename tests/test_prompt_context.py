from __future__ import annotations

import unittest

import main


class PromptContextTests(unittest.IsolatedAsyncioTestCase):
    def test_french_kickoff_asks_only_for_spelled_first_name(self) -> None:
        kickoff = main._kickoff_prompt_for_language(
            "fr",
            "generic",
            "Sonia",
            ["record_caller_details"],
        )

        self.assertIn("uniquement son prénom", kickoff)
        self.assertIn("demandez-lui de l'épeler", kickoff)
        self.assertIn(
            "Ne demandez pas encore son nom, son téléphone, son e-mail",
            kickoff,
        )
        self.assertNotIn(
            "Ne demandez pas d'abord l'email",
            kickoff,
        )

    async def test_caller_record_tool_gets_contact_first_gate(self) -> None:
        instructions = await main._instructions_with_context(
            "Vous êtes Sonia.",
            {
                "enabled_tool_names": ["record_caller_details"],
                "end_user_id": "",
            },
        )

        self.assertIn(
            "COLLECTE OBLIGATOIRE DES COORDONNÉES AU DÉBUT DE L’APPEL",
            instructions,
        )
        self.assertIn(
            "first_name, last_name, phone_number et email",
            instructions,
        )
        self.assertIn(
            "ces champs seront produits automatiquement après l’appel",
            instructions,
        )
        self.assertNotIn(
            "If the caller says thank you, says they are done",
            instructions,
        )

    async def test_other_agents_keep_existing_closing_behavior(self) -> None:
        instructions = await main._instructions_with_context(
            "You are a helpful agent.",
            {
                "enabled_tool_names": ["search_business_knowledge"],
                "end_user_id": "",
            },
        )

        self.assertIn(
            "If the caller says thank you, says they are done",
            instructions,
        )
        self.assertNotIn(
            "COLLECTE OBLIGATOIRE DES COORDONNÉES AU DÉBUT DE L’APPEL",
            instructions,
        )


if __name__ == "__main__":
    unittest.main()
