import logging
import os

from livekit.agents import Agent, RunContext, function_tool

from .ops_api import (
    create_complaint_ticket as create_complaint_ticket_api,
    create_meter_request as create_meter_request_api,
    escalate_issue as escalate_issue_api,
    get_payment_summary as get_payment_summary_api,
    get_tariff_profile as get_tariff_profile_api,
    get_vending_history as get_vending_history_api,
    lookup_customer_account as lookup_customer_account_api,
    report_outage as report_outage_api,
)

logger = logging.getLogger(__name__)
AGENT_CLIENT_ID = os.getenv("AGENT_CLIENT_ID", "sales-girl-internal")
AGENT_NAME = os.getenv("AGENT_NAME", "sales-girl-agent-fr")


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


class SalonAgentFR(Agent):
    """
    Agent de support client EKEDC (francais).
    """

    @function_tool()
    async def lookup_customer_account(
        self,
        ctx: RunContext,
    ) -> dict:
        """Recuperer le profil de compte du client authentifie."""
        return await lookup_customer_account_api(metadata=_tool_metadata(ctx))

    @function_tool()
    async def get_tariff_profile(
        self,
        ctx: RunContext,
    ) -> dict:
        """Recuperer la bande tarifaire, le type de compteur et les details de service du client."""
        return await get_tariff_profile_api(metadata=_tool_metadata(ctx))

    @function_tool()
    async def get_payment_summary(
        self,
        ctx: RunContext,
    ) -> dict:
        """Recuperer l'historique recent des factures et paiements du client."""
        return await get_payment_summary_api(metadata=_tool_metadata(ctx))

    @function_tool()
    async def get_vending_history(
        self,
        ctx: RunContext,
    ) -> dict:
        """Recuperer l'historique recent des achats de token et du compteur."""
        return await get_vending_history_api(metadata=_tool_metadata(ctx))

    @function_tool()
    async def create_complaint_ticket(
        self,
        ctx: RunContext,
        title: str,
        description: str,
        priority: str = "high",
        case_reference: str | None = None,
    ) -> dict:
        """Creer un ticket de plainte pour un probleme de facturation, technique ou de compte."""
        result = await create_complaint_ticket_api(
            title=title,
            description=description,
            priority=priority,
            case_reference=case_reference,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info("[TOOL] create_complaint_ticket title=%s case_reference=%s", title, case_reference)
        return result

    @function_tool()
    async def report_outage(
        self,
        ctx: RunContext,
        summary: str,
        priority: str = "high",
    ) -> dict:
        """Signaler une panne de courant ou un probleme de basse tension."""
        result = await report_outage_api(
            summary=summary,
            priority=priority,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info("[TOOL] report_outage priority=%s", priority)
        return result

    @function_tool()
    async def create_meter_request(
        self,
        ctx: RunContext,
        summary: str,
        priority: str = "normal",
    ) -> dict:
        """Creer une demande liee au compteur, a l'installation ou a la programmation."""
        result = await create_meter_request_api(
            summary=summary,
            priority=priority,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info("[TOOL] create_meter_request priority=%s", priority)
        return result

    @function_tool()
    async def escalate_issue(
        self,
        ctx: RunContext,
        title: str,
        description: str,
        priority: str = "high",
        case_reference: str | None = None,
    ) -> dict:
        """Escalader les problemes qui exigent une intervention humaine ou terrain."""
        result = await escalate_issue_api(
            title=title,
            description=description,
            priority=priority,
            case_reference=case_reference,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info("[TOOL] escalate_issue title=%s case_reference=%s", title, case_reference)
        return result
