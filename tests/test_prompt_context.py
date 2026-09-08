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
            "Après confirmation des quatre champs",
            instructions,
        )
        self.assertIn(
            "ces champs seront produits automatiquement après l’appel",
            instructions,
        )
        self.assertIn(
            "Ne dites pas que vous avez appelé un outil",
            instructions,
        )
        self.assertNotIn(
            "N’appelez record_caller_details",
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

    def test_post_call_caller_marker_is_hidden_from_tool_guidance(self) -> None:
        guidance = main._runtime_tool_guidance(
            {
                "tools": [
                    {
                        "name": "record_caller_details",
                        "description": "Internal marker for post-call caller intake.",
                        "url": "http://conversation-service:8091/v1/tools/caller-contacts",
                    },
                    {
                        "name": "create_ticket",
                        "description": "Create a support ticket.",
                        "url": "http://conversation-service:8091/v1/tickets",
                    },
                ]
            },
            "generic",
        )

        self.assertNotIn("record_caller_details", guidance)
        self.assertIn("create_ticket is enabled", guidance)


if __name__ == "__main__":
    unittest.main()
