import logging
import os

from livekit.agents import Agent, RunContext, function_tool

from .ops_api import (
    create_escalation_ticket as create_escalation_ticket_api,
    dispatch_passport_now as dispatch_passport_now_api,
    issue_certificate_now as issue_certificate_now_api,
    resolve_customer as resolve_customer_api,
    search_certificate_request as search_certificate_request_api,
    search_passport_application as search_passport_application_api,
)

logger = logging.getLogger(__name__)
AGENT_CLIENT_ID = os.getenv("AGENT_CLIENT_ID", "sales-girl-internal")
AGENT_NAME = os.getenv("AGENT_NAME", "sales-girl-agent-en")


def _tool_metadata(ctx: RunContext) -> dict:
    session_userdata = getattr(getattr(ctx, "session", None), "userdata", None)
    if not isinstance(session_userdata, dict):
        session_userdata = {}

    room_name = getattr(getattr(ctx, "room", None), "name", "")
    conversation_id = str(session_userdata.get("conversation_id") or room_name)
    session_id = str(session_userdata.get("session_id") or conversation_id)
    return {
        "client_id": AGENT_CLIENT_ID,
        "agent_id": AGENT_NAME,
        "business_id": str(session_userdata.get("business_id") or ""),
        "conversation_id": conversation_id,
        "session_id": session_id,
        "end_user_id": str(session_userdata.get("end_user_id") or ""),
        "turn_index": int(session_userdata.get("turn_index", 0)),
        "last_user_transcript": str(session_userdata.get("last_user_transcript") or ""),
        "last_assistant_message": str(session_userdata.get("last_assistant_message") or ""),
        "timeline_event_index": int(session_userdata.get("timeline_event_index", 0)),
    }


def _intake_description(ctx: RunContext, *, request_type: str) -> str:
    md = _tool_metadata(ctx)
    customer_email = str(md.get("end_user_id") or "").strip()
    last_user_text = str(md.get("last_user_transcript") or "").strip()
    if request_type == "certificate":
        action = "start a new certificate application"
    else:
        action = "start a new passport application"
    return (
        f"Customer requested to {action}. "
        f"Authenticated email: {customer_email or '-unknown-'}. "
        f"Latest caller message: {last_user_text or '-none-'}."
    )


class SalonAgentEN(Agent):
    """
    Benin call-center assistant agent (English).
    """

    @function_tool()
    async def resolve_caller(
        self,
        ctx: RunContext,
    ) -> dict:
        """Resolve the currently authenticated caller profile from session identity."""
        inferred = str(getattr(getattr(ctx, "session", None), "userdata", {}).get("end_user_id") or "").strip()
        if not inferred:
            return {"status": "failed", "message": "Authenticated caller identity is missing."}
        return await resolve_customer_api(metadata=_tool_metadata(ctx))

    @function_tool()
    async def check_passport_application(
        self,
        ctx: RunContext,
        application_id: str | None = None,
    ) -> dict:
        """Check passport application and dispatch status for the authenticated caller."""
        return await search_passport_application_api(
            application_id=application_id,
            metadata=_tool_metadata(ctx),
        )

    @function_tool()
    async def check_certificate_request(
        self,
        ctx: RunContext,
        certificate_id: str | None = None,
    ) -> dict:
        """Check certificate request status for the authenticated caller."""
        return await search_certificate_request_api(
            certificate_id=certificate_id,
            metadata=_tool_metadata(ctx),
        )

    @function_tool()
    async def dispatch_passport_now(
        self,
        ctx: RunContext,
        application_id: str | None = None,
    ) -> dict:
        """Trigger passport dispatch for the authenticated caller's application (auto-select latest eligible if ID not provided)."""
        result = await dispatch_passport_now_api(
            application_id=application_id,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info("[TOOL] dispatch_passport_now application_id=%s", application_id)
        return result

    @function_tool()
    async def issue_certificate_now(
        self,
        ctx: RunContext,
        certificate_id: str | None = None,
    ) -> dict:
        """Issue certificate for the authenticated caller (auto-select latest eligible if ID not provided)."""
        result = await issue_certificate_now_api(
            certificate_id=certificate_id,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info("[TOOL] issue_certificate_now certificate_id=%s", certificate_id)
        return result

    @function_tool()
    async def create_escalation_ticket(
        self,
        ctx: RunContext,
        title: str,
        description: str,
        priority: str = "high",
        case_reference: str | None = None,
    ) -> dict:
        """Create a CRM escalation ticket for the authenticated caller."""
        inferred = str(getattr(getattr(ctx, "session", None), "userdata", {}).get("end_user_id") or "").strip()
        if not inferred:
            return {"status": "failed", "message": "Authenticated caller identity is required for escalation."}
        return await create_escalation_ticket_api(
            title=title,
            description=description,
            priority=priority,
            case_reference=case_reference,
            metadata=_tool_metadata(ctx),
        )

    @function_tool()
    async def start_certificate_application(
        self,
        ctx: RunContext,
    ) -> dict:
        """Create a human-handled intake ticket to start a new certificate application."""
        inferred = str(getattr(getattr(ctx, "session", None), "userdata", {}).get("end_user_id") or "").strip()
        if not inferred:
            return {"status": "failed", "message": "Authenticated caller identity is required for intake."}
        return await create_escalation_ticket_api(
            title="New certificate application request",
            description=_intake_description(ctx, request_type="certificate"),
            priority="high",
            metadata=_tool_metadata(ctx),
        )

    @function_tool()
    async def start_passport_application(
        self,
        ctx: RunContext,
    ) -> dict:
        """Create a human-handled intake ticket to start a new passport application."""
        inferred = str(getattr(getattr(ctx, "session", None), "userdata", {}).get("end_user_id") or "").strip()
        if not inferred:
            return {"status": "failed", "message": "Authenticated caller identity is required for intake."}
        return await create_escalation_ticket_api(
            title="New passport application request",
            description=_intake_description(ctx, request_type="passport"),
            priority="high",
            metadata=_tool_metadata(ctx),
        )
