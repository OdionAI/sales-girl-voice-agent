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


def _intake_description(ctx: RunContext, *, request_type: str) -> str:
    md = _tool_metadata(ctx)
    customer_email = str(md.get("end_user_id") or "").strip()
    last_user_text = str(md.get("last_user_transcript") or "").strip()
    if request_type == "certificate":
        action = "demarrer une nouvelle demande de certificat"
    else:
        action = "demarrer une nouvelle demande de passeport"
    return (
        f"Le client souhaite {action}. "
        f"Email authentifie : {customer_email or '-inconnu-'}. "
        f"Dernier message du client : {last_user_text or '-aucun-'}."
    )


class SalonAgentFR(Agent):
    """
    Assistant de centre d'appels pour les operations consulaires (francais).
    """

    @function_tool()
    async def resolve_caller(
        self,
        ctx: RunContext,
    ) -> dict:
        """Recuperer le profil de l'appelant authentifie a partir de l'identite de session."""
        inferred = str(getattr(getattr(ctx, "session", None), "userdata", {}).get("end_user_id") or "").strip()
        if not inferred:
            return {"status": "failed", "message": "L'identite authentifiee de l'appelant est absente."}
        return await resolve_customer_api(metadata=_tool_metadata(ctx))

    @function_tool()
    async def check_passport_application(
        self,
        ctx: RunContext,
        application_id: str | None = None,
    ) -> dict:
        """Verifier l'etat de la demande de passeport et le statut d'expedition de l'appelant authentifie."""
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
        """Verifier l'etat de la demande de certificat de l'appelant authentifie."""
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
        """Declencher l'expedition du passeport de l'appelant authentifie (selection automatique si aucun identifiant n'est fourni)."""
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
        """Emettre le certificat de l'appelant authentifie (selection automatique si aucun identifiant n'est fourni)."""
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
        """Creer un ticket CRM d'escalade pour l'appelant authentifie."""
        inferred = str(getattr(getattr(ctx, "session", None), "userdata", {}).get("end_user_id") or "").strip()
        if not inferred:
            return {"status": "failed", "message": "L'identite authentifiee est requise pour l'escalade."}
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
        """Creer un ticket d'intake humain pour demarrer une nouvelle demande de certificat."""
        inferred = str(getattr(getattr(ctx, "session", None), "userdata", {}).get("end_user_id") or "").strip()
        if not inferred:
            return {"status": "failed", "message": "L'identite authentifiee est requise pour l'intake."}
        return await create_escalation_ticket_api(
            title="Nouvelle demande de certificat",
            description=_intake_description(ctx, request_type="certificate"),
            priority="high",
            metadata=_tool_metadata(ctx),
        )

    @function_tool()
    async def start_passport_application(
        self,
        ctx: RunContext,
    ) -> dict:
        """Creer un ticket d'intake humain pour demarrer une nouvelle demande de passeport."""
        inferred = str(getattr(getattr(ctx, "session", None), "userdata", {}).get("end_user_id") or "").strip()
        if not inferred:
            return {"status": "failed", "message": "L'identite authentifiee est requise pour l'intake."}
        return await create_escalation_ticket_api(
            title="Nouvelle demande de passeport",
            description=_intake_description(ctx, request_type="passport"),
            priority="high",
            metadata=_tool_metadata(ctx),
        )
