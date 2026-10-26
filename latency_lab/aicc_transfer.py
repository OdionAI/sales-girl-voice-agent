"""Pinned AICC bridge destination; never dial a number supplied by the LLM."""
import asyncio
import os
from .sip_routing import normalize_phone
from livekit import api

async def bridge_call(client, *, room_name, session_id, reason_summary, caller_phone="", config=None):
    cfg = os.environ if config is None else config
    trunk = str(cfg.get('AICC_OUTBOUND_TRUNK_ID', '')).strip()
    target = str(cfg.get('AICC_TRANSFER_TARGET_NUMBER', '')).strip()
    preserve = str(cfg.get('AICC_TRANSFER_PRESERVE_CALLER_ID', '')).lower() == 'true'
    caller = normalize_phone(caller_phone) if preserve else str(cfg.get('AICC_TRANSFER_FROM_NUMBER', '')).strip()
    if preserve and not caller:
        return {'status': 'failed', 'error': 'verified_sip_caller_required'}
    if not room_name or not trunk or not target or not caller:
        return {'status': 'failed', 'error': 'aicc_destination_not_configured'}
    identity = 'aicc_bridge_' + session_id
    request = api.CreateSIPParticipantRequest(
        sip_trunk_id=trunk, sip_call_to=target, sip_number=caller,
        room_name=room_name, participant_identity=identity, participant_name='AICC colleague',
        wait_until_answered=True, play_dialtone=True,
        headers={'X-Odion-Session-ID': session_id},
        participant_attributes={'call_role': 'aicc_bridge'},
    )
    request.ringing_timeout.seconds = 40
    try:
        participant = await asyncio.wait_for(client.sip.create_sip_participant(request), timeout=50)
        return {'status': 'connected', 'participant_identity': participant.participant_identity or identity,
                'sip_call_id': participant.sip_call_id, 'target_number': target}
    except (Exception, asyncio.CancelledError) as exc:
        # An uncertain timeout must not leave an orphan leg that may connect later.
        try:
            await asyncio.wait_for(client.room.remove_participant(api.RoomParticipantIdentity(
                room=room_name, identity=identity)), timeout=5)
        except Exception:
            pass
        if isinstance(exc, asyncio.CancelledError):
            raise
        return {'status': 'failed', 'error': type(exc).__name__,
                'sip_status_code': str(getattr(exc, 'metadata', {}).get('sip_status_code', ''))}
