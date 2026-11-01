"""Isolated fixture: unmodified Conversation capacity router + local auth/models.
Never import this into a production service. PYTHONPATH points at a pinned,
unchanged Conversation-service checkout; DATABASE_URL must name a fresh local DB.
"""
import os
from fastapi import FastAPI, Header, HTTPException, WebSocket
from fastapi.responses import JSONResponse
from sqlalchemy import select
from app.api.routes_capacity import router
from app.config.settings import get_settings
from app.db.base import Base
from app.db.session import engine, SessionLocal
from app.models.call_capacity import CallCapacityReservation

BUSINESS = '11111111-2222-3333-4444-555555555555'
AGENT = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
TOKEN = 'local-internal-service-token'
if not os.environ['DATABASE_URL'].startswith('sqlite+pysqlite:////private/tmp/sg-capacity-'):
    raise RuntimeError('Capacity fixture requires an isolated local SQLite database')
Base.metadata.create_all(engine)
app = FastAPI()
app.include_router(router)
events = []

@app.middleware('http')
async def record(request, call_next):
    response = await call_next(request)
    if '/capacity/reservations' in request.url.path:
        events.append({'path': request.url.path, 'status': response.status_code})
    return response

@app.post('/v1/internal/aicc/outbound/authorize')
def authorize(body: dict, x_aicc_api_key: str = Header(''), x_service_token: str = Header('')):
    if x_aicc_api_key != 'aicc-local-fixture' or x_service_token != TOKEN or body.get('agent_id') != 'agt_capacity_fixture':
        raise HTTPException(403)
    return {'business_id': BUSINESS, 'agent_id': AGENT, 'agent_public_id': 'agt_capacity_fixture', 'agent_name': 'Capacity Review'}

@app.get('/v1/agents/internal/agents/{agent}/runtime-config')
def runtime(agent: str, x_service_token: str = Header('')):
    if x_service_token != TOKEN or agent != AGENT:
        raise HTTPException(403)
    return {'business_id': BUSINESS, 'agent_id': AGENT, 'name': 'Capacity Review',
            'instructions': 'Local capacity lifecycle test.', 'tts_language_hint': 'en',
            'tools': [], 'knowledge_base_ids': []}

@app.get('/v1/agents/internal/sip-routes/resolve')
def resolve():
    return JSONResponse({'detail': 'Use the pinned local fixture route'}, status_code=404)

@app.websocket('/asr')
async def asr(ws: WebSocket):
    await ws.accept()
    await ws.send_json({'type': 'session.created'})
    try:
        while True:
            await ws.receive_text()
    except Exception:
        return

@app.get('/fixture/state')
def state():
    with SessionLocal() as db:
        rows = db.scalars(select(CallCapacityReservation)).all()
        return {'rows': [{'status': r.status, 'room': r.room_name, 'id': r.reservation_ref} for r in rows],
                'events': events, 'limit': get_settings().momo_call_capacity_max_concurrent}

@app.post('/fixture/fallback/{target}')
def fallback(target: str, body: dict):
    if target not in {'human', 'ivr'}:
        raise HTTPException(400)
    events.append({'fallback': target, 'attempt': body['attempt_id']})
    return {'routed': target, 'simulated': True}

# Account/auth/billing are local fixtures; the capacity router above is unchanged.
@app.get('/v1/public/businesses/{slug}/agents/{public_id}')
def public_agent(slug: str, public_id: str):
    if slug != 'capacity-review' or public_id != 'agt_capacity_fixture':
        raise HTTPException(404)
    return {'business': {'id': BUSINESS, 'slug': slug, 'name': 'Capacity Review'},
            'config_agent_id': AGENT, 'agent_public_id': public_id,
            'configured_agent_name': 'Capacity Review', 'voice_auth_required': False,
            'runtime_agent_name_en': 'existing-account-en', 'default_language': 'en'}

@app.get('/v1/public/resolve-agent')
def resolve_host(host: str):
    return public_agent('capacity-review', 'agt_capacity_fixture')

@app.get('/v1/auth/me')
def me(authorization: str = Header('')):
    if authorization != 'Bearer local-owner-fixture':
        raise HTTPException(401)
    return {'business': {'id': BUSINESS}, 'user': {'name': 'Local Owner', 'email': 'owner@example.test'}}

@app.get('/v1/agents')
def agents(authorization: str = Header('')):
    me(authorization)
    return {'items': [{'id': AGENT, 'name': 'Capacity Review', 'is_active': True, 'voice_auth_required': False}]}

@app.post('/v1/internal/credits/authorize-call')
def billing(body: dict, x_business_id: str = Header(''), x_service_token: str = Header('')):
    if x_business_id != BUSINESS or x_service_token != TOKEN:
        raise HTTPException(403)
    events.append({'billing_caller': body.get('end_user_id')})
    if body.get('end_user_id') == 'no-airtime@example.test':
        return JSONResponse({'code': 'no_airtime', 'authorized': False}, status_code=402)
    return {'authorized': True}
