import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np

from agent.auth_observer import (
    AUTH_FAILED,
    AUTH_PENDING,
    AUTH_VERIFIED,
    AUTH_VERIFIED_HINT,
    FakeVoiceAuthObserver,
    apply_auth_observer_session,
)
from agent.salon_agent import _unverified_email_result, _voice_auth_status


def _match(matched: bool, enrolled: bool = True, reason: str = "") -> dict:
    return {
        "matched": matched,
        "enrolled": enrolled,
        "score": 0.91 if matched else 0.21,
        "threshold": 0.75,
        "reason": reason or ("" if matched else "below_threshold"),
    }


class _FakeSession:
    def __init__(self, userdata=None) -> None:
        self.userdata = userdata or {
            "auth_observer_enabled": True,
            "auth_status": AUTH_PENDING,
            "end_user_id": "caller@example.com",
            "base_instructions": "You are a helpful agent.",
        }
        self.current_agent = SimpleNamespace(update_instructions=AsyncMock())
        self._listeners: dict[str, list] = {}

    def on(self, event_name: str):
        def decorator(func):
            self._listeners.setdefault(event_name, []).append(func)
            return func

        return decorator

    def emit(self, event_name: str, event: object) -> None:
        for listener in self._listeners.get(event_name, []):
            listener(event)


class AuthObserverLogicTests(unittest.TestCase):
    def test_apply_session_uses_wema_persona_and_keeps_auth_pending(self) -> None:
        userdata = {"enabled_tool_names": ["search_business_knowledge"], "end_user_id": "a@b.com"}
        instructions = apply_auth_observer_session(userdata, "You are a helpful agent.")

        self.assertTrue(userdata["auth_observer_enabled"])
        self.assertEqual(userdata["auth_status"], AUTH_PENDING)
        self.assertEqual(userdata["action_auth_status"], AUTH_PENDING)
        self.assertIn("complete_airtime_purchase", userdata["enabled_tool_names"])
        self.assertIn("complete_funds_transfer", userdata["enabled_tool_names"])
        self.assertIn("Wema", instructions)
        self.assertIn("recognize", instructions)
        self.assertIn("confirm", instructions)
        self.assertNotIn("verify me", instructions)
        self.assertNotIn("You are a helpful agent.", instructions)

    def test_send_email_stays_blocked_until_verified(self) -> None:
        ctx = SimpleNamespace(
            session=SimpleNamespace(
                userdata={
                    "auth_observer_enabled": True,
                    "auth_status": AUTH_PENDING,
                }
            )
        )
        self.assertEqual(_voice_auth_status(ctx), AUTH_PENDING)
        blocked = _unverified_email_result(AUTH_PENDING)
        self.assertEqual(blocked["status"], "failed")
        self.assertTrue(blocked["auth_required"])

        ctx.session.userdata["auth_status"] = AUTH_VERIFIED
        self.assertEqual(_voice_auth_status(ctx), AUTH_VERIFIED)

    def test_observer_verifies_session_once_from_first_usable_clip(self) -> None:
        session = _FakeSession()
        compares = [_match(True)]

        async def _run() -> None:
            observer = FakeVoiceAuthObserver(
                session, delay_seconds=0, compare_fn=lambda *args, **kwargs: compares.pop(0)
            )
            await observer.ingest_pcm(np.ones(16000 * 2, dtype=np.float32), 16000)
            session.emit(
                "conversation_item_added",
                SimpleNamespace(item=SimpleNamespace(role="user", content="Hello, I need help with airtime")),
            )
            await asyncio.sleep(0.05)
            session.emit(
                "conversation_item_added",
                SimpleNamespace(item=SimpleNamespace(role="user", content="Also what is my data balance")),
            )
            await asyncio.sleep(0.05)

        asyncio.run(_run())
        self.assertEqual(session.userdata["auth_status"], AUTH_VERIFIED)
        self.assertEqual(compares, [])
        session.current_agent.update_instructions.assert_awaited()
        injected = session.current_agent.update_instructions.await_args.args[0]
        self.assertIn("VERIFIED", injected)
        self.assertIn("VERIFIED", AUTH_VERIFIED_HINT)

    def test_injected_kickoff_does_not_verify_session(self) -> None:
        session = _FakeSession()

        async def _run() -> None:
            observer = FakeVoiceAuthObserver(
                session,
                delay_seconds=0,
                compare_fn=lambda *args, **kwargs: _match(True),
            )
            await observer.ingest_pcm(np.ones(16000 * 2, dtype=np.float32), 16000)
            session.emit(
                "conversation_item_added",
                SimpleNamespace(
                    item=SimpleNamespace(
                        role="user",
                        content="Start the conversation now. Greet the caller first in English.",
                    )
                ),
            )
            await asyncio.sleep(0.05)
            self.assertEqual(session.userdata["auth_status"], AUTH_PENDING)

        asyncio.run(_run())

    def test_open_mic_does_not_verify_session_before_user_speech(self) -> None:
        session = _FakeSession()

        async def _run() -> None:
            observer = FakeVoiceAuthObserver(
                session,
                delay_seconds=0,
                compare_fn=lambda *args, **kwargs: _match(True),
            )
            await observer.ingest_pcm(np.ones(16000 * 3, dtype=np.float32), 16000)
            await asyncio.sleep(0.05)
            self.assertEqual(session.userdata["auth_status"], AUTH_PENDING)

        asyncio.run(_run())

    def test_observer_fails_session_when_voice_does_not_match(self) -> None:
        session = _FakeSession()

        async def _run() -> None:
            observer = FakeVoiceAuthObserver(
                session, delay_seconds=0, compare_fn=lambda *args, **kwargs: _match(False)
            )
            await observer.ingest_pcm(np.ones(16000 * 2, dtype=np.float32), 16000)
            session.emit(
                "conversation_item_added",
                SimpleNamespace(item=SimpleNamespace(role="user", content="Hello there")),
            )
            await asyncio.sleep(0.05)

        asyncio.run(_run())
        self.assertEqual(session.userdata["auth_status"], AUTH_FAILED)

    def test_action_check_is_independent_and_gates_airtime(self) -> None:
        session = _FakeSession()
        results = [_match(True), _match(False), _match(True)]

        async def _run() -> None:
            observer = FakeVoiceAuthObserver(
                session,
                delay_seconds=0,
                compare_fn=lambda *args, **kwargs: results.pop(0),
            )
            await observer.ingest_pcm(np.ones(16000 * 2, dtype=np.float32), 16000)
            session.emit(
                "conversation_item_added",
                SimpleNamespace(item=SimpleNamespace(role="user", content="Hi, I want to check my number")),
            )
            await asyncio.sleep(0.05)
            self.assertEqual(session.userdata["auth_status"], AUTH_VERIFIED)
            blocked = await observer.authorize_action(
                action="complete_airtime_purchase",
                transcript="buy 500 naira airtime",
            )
            allowed = await observer.authorize_action(
                action="complete_airtime_purchase",
                transcript="please buy 500 naira airtime now",
            )
            self.assertFalse(blocked["authorized"])
            self.assertEqual(blocked["reason"], "action_check_failed")
            self.assertEqual(blocked["action_status"], AUTH_FAILED)
            self.assertTrue(allowed["authorized"])
            self.assertEqual(allowed["outcome"], "completed")
            self.assertEqual(session.userdata["auth_status"], AUTH_VERIFIED)
            self.assertEqual(session.userdata["action_auth_status"], AUTH_VERIFIED)

        asyncio.run(_run())
        self.assertEqual(results, [])

    def test_action_check_blocks_when_session_is_unauthenticated(self) -> None:
        session = _FakeSession()
        results = [_match(False, enrolled=False, reason="not_enrolled"), _match(True)]

        async def _run() -> None:
            observer = FakeVoiceAuthObserver(
                session,
                delay_seconds=0,
                compare_fn=lambda *args, **kwargs: results.pop(0),
            )
            decision = await observer.authorize_action(
                action="complete_airtime_purchase",
                transcript="buy airtime",
            )
            self.assertFalse(decision["authorized"])
            self.assertEqual(decision["reason"], "session_unauthenticated")
            self.assertEqual(decision["session_status"], AUTH_FAILED)
            self.assertEqual(decision["action_status"], AUTH_VERIFIED)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
