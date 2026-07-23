from __future__ import annotations

import unittest

import main


class ConversationPersistenceContractTests(unittest.TestCase):
    def test_domain_statuses_are_successful_writes(self) -> None:
        for status in ("success", "active", "ended", "ready", "available"):
            with self.subTest(status=status):
                self.assertFalse(main._conversation_write_failed({"status": status}))

    def test_transport_failure_statuses_are_failed_writes(self) -> None:
        for status in ("failed", "disabled", "error", ""):
            with self.subTest(status=status):
                self.assertTrue(main._conversation_write_failed({"status": status}))

        self.assertTrue(main._conversation_write_failed(None))


if __name__ == "__main__":
    unittest.main()
