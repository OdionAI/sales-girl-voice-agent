from __future__ import annotations

from datetime import timedelta
import unittest
from unittest.mock import AsyncMock, patch

import main
from agent import billing_hooks


class BillingHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_call_heartbeat_posts_expected_payload(self) -> None:
        with patch("agent.billing_hooks._post", new=AsyncMock(return_value={"status": "success"})) as post:
            result = await billing_hooks.send_call_heartbeat(
                conversation_id="conv-1",
                session_id="session-1",
                end_user_id="user-1",
                duration_seconds=12,
                idempotency_key="hb-12",
                channel="voice",
                business_id="biz-1",
            )

        self.assertEqual(result["status"], "success")
        post.assert_awaited_once_with(
            "/v1/internal/credits/heartbeat",
            {
                "conversation_id": "conv-1",
                "session_id": "session-1",
                "end_user_id": "user-1",
                "duration_seconds": 12,
                "idempotency_key": "hb-12",
                "channel": "voice",
            },
            business_id="biz-1",
        )

    async def test_heartbeat_exhaustion_requests_session_shutdown(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.shutdown_calls = []

            def shutdown(self, *, drain: bool = True) -> None:
                self.shutdown_calls.append({"drain": drain})

        class FakeContext:
            def __init__(self) -> None:
                self.shutdown_reason = None
                self.deleted = False

            def shutdown(self, reason: str = "") -> None:
                self.shutdown_reason = reason

            def delete_room(self):  # noqa: ANN001
                self.deleted = True
                return None

        session = FakeSession()
        ctx = FakeContext()
        userdata = {
            "business_id": "biz-1",
            "conversation_id": "conv-1",
            "session_id": "local-session",
            "session_tracker_id": "tracked-session",
            "end_user_id": "user-1",
        }

        with (
            patch("main.asyncio.sleep", new=AsyncMock(return_value=None)),
            patch(
                "main.send_billing_call_heartbeat",
                new=AsyncMock(
                    return_value={
                        "status": "exhausted",
                        "should_end_call": True,
                        "balance_kobo": 0,
                    }
                ),
            ) as heartbeat,
        ):
            await main._billing_heartbeat_loop(
                session=session,
                ctx=ctx,
                userdata=userdata,
                business_id="biz-1",
                started_at=main.conv_api_utcnow() - timedelta(seconds=12),
                call_channel="voice",
            )

        self.assertEqual(session.shutdown_calls, [{"drain": True}])
        self.assertEqual(ctx.shutdown_reason, "billing_exhausted")
        self.assertTrue(ctx.deleted)
        heartbeat.assert_awaited_once()
        self.assertEqual(heartbeat.await_args.kwargs["session_id"], "tracked-session")

    async def test_authorization_failure_blocks_when_fail_closed_enabled(self) -> None:
        userdata = {
            "conversation_id": "conv-1",
            "session_id": "local-session",
            "end_user_id": "user-1",
        }

        with (
            patch("main.BILLING_FAIL_CLOSED", True),
            patch("main.billing_hooks_enabled", return_value=True),
            patch(
                "main.authorize_billing_call_start",
                new=AsyncMock(
                    return_value={
                        "status": "failed",
                        "detail": "billing authorization unavailable",
                    }
                ),
            ) as authorize,
        ):
            with self.assertRaisesRegex(RuntimeError, "billing authorization unavailable"):
                await main._authorize_billing_start_or_raise(
                    userdata=userdata,
                    business_id="biz-1",
                    call_channel="voice",
                )

        authorize.assert_awaited_once()

    async def test_final_billing_report_runs_once(self) -> None:
        userdata = {
            "business_id": "biz-1",
            "conversation_id": "conv-1",
            "session_id": "local-session",
            "session_tracker_id": "tracked-session",
            "end_user_id": "user-1",
        }

        with (
            patch("main.billing_hooks_enabled", return_value=True),
            patch("main.report_billing_call_usage", new=AsyncMock(return_value={"status": "success"})) as report,
        ):
            await main._report_billing_final_usage(
                userdata=userdata,
                business_id="biz-1",
                duration_seconds=42,
                call_channel="voice",
            )
            await main._report_billing_final_usage(
                userdata=userdata,
                business_id="biz-1",
                duration_seconds=42,
                call_channel="voice",
            )

        report.assert_awaited_once()
        self.assertEqual(report.await_args.kwargs["session_id"], "tracked-session")
        self.assertEqual(report.await_args.kwargs["duration_seconds"], 42)


if __name__ == "__main__":
    unittest.main()
