from __future__ import annotations

import unittest

import main


class PromptContextTests(unittest.IsolatedAsyncioTestCase):
    def test_french_kickoff_greets_before_collecting_identifiers(self) -> None:
        kickoff = main._kickoff_prompt_for_language("fr", "generic")
        self.assertIn("Saluez l'appelant en français", kickoff)
        self.assertIn("Ne demandez pas d'abord l'email", kickoff)
        self.assertIn("saluez d'abord puis attendez sa demande", kickoff)
        self.assertNotIn("demandez-lui de l'épeler", kickoff)

    async def test_caller_record_configuration_does_not_force_contact_intake(self) -> None:
        instructions = await main._instructions_with_context(
            "Vous êtes Sonia.",
            {"enabled_tool_names": ["record_caller_details"], "end_user_id": ""},
        )
        self.assertIn("Vous êtes Sonia.", instructions)
        self.assertIn("If the caller says thank you, says they are done", instructions)
        self.assertNotIn("COLLECTE OBLIGATOIRE", instructions)

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

    def test_enabled_caller_tool_uses_its_configured_guidance(self) -> None:
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

        self.assertIn("record_caller_details is enabled", guidance)
        self.assertIn("Internal marker for post-call caller intake.", guidance)
        self.assertIn("create_ticket is enabled", guidance)


if __name__ == "__main__":
    unittest.main()
