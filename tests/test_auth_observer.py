import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import numpy as np

from agent.auth_observer import (
    ACTION_WEMA_EXECUTE_PREPARED,
    AUTH_FAILED,
    AUTH_PENDING,
    AUTH_VERIFIED,
    AUTH_VERIFIED_HINT,
    FakeVoiceAuthObserver,
    apply_auth_observer_session,
)


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
    def test_apply_session_preserves_agent_prompt_and_keeps_auth_pending(self) -> None:
        userdata = {
            "enabled_tool_names": [
                "search_business_knowledge",
                ACTION_WEMA_EXECUTE_PREPARED,
            ],
            "end_user_id": "a@b.com",
        }
        with patch.dict(os.environ, {"AUTH_OBSERVER_ENABLED": "true"}):
            instructions = apply_auth_observer_session(
                userdata, "You are a helpful agent."
            )

        self.assertTrue(userdata["auth_observer_enabled"])
        self.assertEqual(userdata["auth_status"], AUTH_PENDING)
        self.assertEqual(userdata["action_auth_status"], AUTH_PENDING)
        self.assertEqual(
            userdata["enabled_tool_names"],
            ["search_business_knowledge", ACTION_WEMA_EXECUTE_PREPARED],
        )
        self.assertIn("You are a helpful agent.", instructions)
        self.assertIn(ACTION_WEMA_EXECUTE_PREPARED, instructions)
        self.assertIn("recognize", instructions)
        self.assertIn("confirm", instructions)

    def test_apply_session_does_not_enable_auth_for_other_agents(self) -> None:
        userdata = {
            "enabled_tool_names": ["search_business_knowledge"],
            "end_user_id": "a@b.com",
        }
        with patch.dict(os.environ, {"AUTH_OBSERVER_ENABLED": "true"}):
            instructions = apply_auth_observer_session(
                userdata, "You are a helpful agent."
            )
        self.assertFalse(userdata["auth_observer_enabled"])
        self.assertEqual(instructions, "You are a helpful agent.")

    def test_apply_session_enables_auth_for_wema_read_tools(self) -> None:
        userdata = {
            "enabled_tool_names": ["wema_get_balance", "wema_get_transactions"],
            "end_user_id": "a@b.com",
        }
        with patch.dict(os.environ, {"AUTH_OBSERVER_ENABLED": "true"}):
            instructions = apply_auth_observer_session(
                userdata, "You are a helpful agent."
            )

        self.assertTrue(userdata["auth_observer_enabled"])
        self.assertIn("every Wema tool call", instructions)

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

    def test_failed_session_check_retries_on_next_user_utterance(self) -> None:
        session = _FakeSession()
        results = [_match(False), _match(True)]

        async def _run() -> None:
            observer = FakeVoiceAuthObserver(
                session,
                delay_seconds=0,
                compare_fn=lambda *args, **kwargs: results.pop(0),
            )
            await observer.ingest_pcm(np.ones(16000 * 2, dtype=np.float32), 16000)
            session.emit(
                "conversation_item_added",
                SimpleNamespace(
                    item=SimpleNamespace(role="user", content="Please check my balance")
                ),
            )
            await asyncio.sleep(0.05)
            self.assertEqual(session.userdata["auth_status"], AUTH_FAILED)

            await observer.ingest_pcm(np.ones(16000 * 2, dtype=np.float32), 16000)
            session.emit(
                "conversation_item_added",
                SimpleNamespace(
                    item=SimpleNamespace(role="user", content="Check it again please")
                ),
            )
            await asyncio.sleep(0.05)

        asyncio.run(_run())
        self.assertEqual(session.userdata["auth_status"], AUTH_VERIFIED)
        self.assertEqual(results, [])

    def test_wema_read_tool_requires_session_and_fresh_action_checks(self) -> None:
        session = _FakeSession()
        results = [_match(True), _match(True)]

        async def _run() -> None:
            observer = FakeVoiceAuthObserver(
                session,
                delay_seconds=0,
                compare_fn=lambda *args, **kwargs: results.pop(0),
            )
            await observer.ingest_pcm(np.ones(16000 * 2, dtype=np.float32), 16000)
            decision = await observer.authorize_action(
                action="wema_get_balance",
                transcript="What is my balance?",
            )
            self.assertTrue(decision["authorized"])
            self.assertEqual(decision["session_status"], AUTH_VERIFIED)
            self.assertEqual(decision["action_status"], AUTH_VERIFIED)

        asyncio.run(_run())
        self.assertEqual(results, [])

    def test_tool_retry_uses_new_utterance_after_failed_session_check(self) -> None:
        session = _FakeSession()
        results = [_match(False), _match(True), _match(True)]

        async def _run() -> None:
            observer = FakeVoiceAuthObserver(
                session,
                delay_seconds=0,
                compare_fn=lambda *args, **kwargs: results.pop(0),
            )
            await observer.ingest_pcm(np.ones(16000 * 2, dtype=np.float32), 16000)
            session.emit(
                "conversation_item_added",
                SimpleNamespace(
                    item=SimpleNamespace(role="user", content="Please check my balance")
                ),
            )
            await asyncio.sleep(0.05)
            self.assertEqual(session.userdata["auth_status"], AUTH_FAILED)

            await observer.ingest_pcm(np.ones(16000 * 2, dtype=np.float32), 16000)
            session.emit(
                "conversation_item_added",
                SimpleNamespace(
                    item=SimpleNamespace(role="user", content="Please try my balance again")
                ),
            )
            decision = await observer.authorize_action(
                action="wema_get_balance",
                transcript="Please try my balance again",
            )
            self.assertTrue(decision["authorized"])
            await asyncio.sleep(0.05)

        asyncio.run(_run())
        self.assertEqual(results, [])

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
                action=ACTION_WEMA_EXECUTE_PREPARED,
                transcript="buy 500 naira airtime",
            )
            allowed = await observer.authorize_action(
                action=ACTION_WEMA_EXECUTE_PREPARED,
                transcript="please buy 500 naira airtime now",
                details={
                    "authorized": "forged",
                    "outcome": "completed",
                    "operation_id": "op-1",
                },
            )
            self.assertFalse(blocked["authorized"])
            self.assertEqual(blocked["reason"], "action_check_failed")
            self.assertEqual(blocked["action_status"], AUTH_FAILED)
            self.assertTrue(allowed["authorized"])
            self.assertEqual(allowed["outcome"], "authorized")
            self.assertEqual(allowed["operation_id"], "op-1")
            completed = await observer.publish_action_outcome(
                action=ACTION_WEMA_EXECUTE_PREPARED,
                tool_result={"status": "success"},
            )
            self.assertEqual(completed["outcome"], "completed")
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
                action=ACTION_WEMA_EXECUTE_PREPARED,
                transcript="buy airtime",
            )
            self.assertFalse(decision["authorized"])
            self.assertEqual(decision["reason"], "session_unauthenticated")
            self.assertEqual(decision["session_status"], AUTH_FAILED)
            self.assertEqual(decision["action_status"], AUTH_VERIFIED)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
