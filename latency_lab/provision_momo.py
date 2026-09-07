"""Create Yinka, the local Momo PSB demo agent, cloning SAW's configured tools."""

import argparse
import asyncio
import hashlib

import aiohttp

from .config import lab_config_from_environ, load_platform_env


MOMO_COMPLAINT_INSTRUCTIONS = (
    "Customer issues and complaints: listen carefully, acknowledge the inconvenience with empathy, "
    "and take note of what happened and what help the customer needs. Use details already given in "
    "the conversation and caller profile. If anything needed to log the issue is missing, ask one "
    "clear follow-up question at a time, usually one to three questions in total. Depending on the "
    "issue, clarify when it happened, the affected service or transaction, any relevant amount or "
    "reference, and the impact on the customer. Do not ask unnecessary questions or request a PIN, "
    "password or OTP. Once there is enough information, call create_ticket with a concise title "
    "and an accurate description of the issue, the customer's answers and the requested follow-up. "
    "Do not merely say you will log it: use the tool. Follow the application's existing confirmation "
    "step before ticket creation. Only after the tool confirms success, tell the customer the issue "
    "has been logged, share the actual returned ticket reference, and assure them they will receive "
    "feedback within twenty-four hours. This is a promise of feedback, not a guarantee of resolution "
    "or a refund within that time. If ticket creation fails, explain that it was not logged and offer "
    "to retry; never invent a ticket reference or claim successful escalation."
)


async def provision(business_id: str, source_id: str) -> dict:
    load_platform_env()
    config = lab_config_from_environ()
    headers = {"X-Service-Token": config.agent_config_service_token,
               "X-Business-ID": business_id, "X-Service-Name": "sales-girl-voice-agent"}
    base = config.agent_config_api_base_url
    async with aiohttp.ClientSession(headers=headers) as session:
        async def request(method, path, **kwargs):
            async with session.request(method, base + path, **kwargs) as response:
                response.raise_for_status()
                return await response.json()
        source = await request("GET", f"/v1/agents/internal/agents/{source_id}/runtime-config")
        tools = await request("GET", f"/v1/agents/{source_id}/tools")
        before = hashlib.sha256(source["instructions"].encode()).hexdigest()
        agents = await request("GET", "/v1/agents?size=200")
        matches = [agent for agent in agents["items"]
                   if agent.get("public_id") == "agt_a9996408aa" or agent["name"] in {"Yinka", "MTN MoMo"}]
        if len(matches) > 1:
            raise RuntimeError("Multiple Momo PSB agent candidates exist; refusing an ambiguous update.")
        if matches:
            agent = matches[0]
        else:
            prompt = (
                "You are Yinka, the Momo PSB voice assistant for this local mobile-money demonstration. "
                "Your name is Yinka. Refer to the institution as Momo PSB. "
                "Introduce yourself as Yinka from Momo PSB, never SAW, Fidelity or Wema. "
                "Speak warm, concise Nigerian English naturally, without forced fillers or numbered lists. "
                "Help with balances, recent transactions, data packages, transfer preparation and support tickets "
                "using only your configured tools. The wema_ tool names refer to the shared staging banking "
                "connector, not separate Momo PSB production APIs. Do not volunteer repeated testing disclaimers, "
                "but answer truthfully if asked about the integration or a blocked transaction. "
                "Never invent balances, packages, fees, bank codes, ticket references or transaction success. "
                "Use the caller profile supplied by the application; do not ask again for known details. "
                "For purchases to my line use the saved phone; for another person ask their number. "
                "Say naira, megabytes and gigabytes in full. Read account and phone numbers digit by digit slowly. "
                "Combine spoken digit words across utterances without guessing missing digits. "
                "For a misheard bank name such as O P, search the current bank list and ask whether the "
                "caller means the closest returned match, for example OPay. "
                "Read the returned transaction preview and ask confirmation before executing it. "
                "Follow the application's session voice-verification policy. "
                "Report tool failures plainly and offer a support ticket. "
            ) + MOMO_COMPLAINT_INSTRUCTIONS
            payload = {"name": "Yinka", "description": "Local Momo PSB demonstration using the same staging connector and caller profile as SAW.",
                       "instructions": prompt, "theme_key": source.get("theme_key", "default")}
            for key in ("tts_provider", "tts_voice_id", "tts_owner_id", "tts_language_scope", "tts_language_hint"):
                if source.get(key) is not None:
                    payload[key] = source[key]
            agent = await request("POST", "/v1/agents", json=payload)
        existing = await request("GET", f"/v1/agents/{agent['id']}/tools")
        names = {tool["name"] for tool in existing}
        for tool in tools:
            if tool["is_active"] and tool["name"] not in names:
                payload = {key: tool[key] for key in ("name", "description", "kind", "method", "url", "headers", "request_schema", "is_active")}
                await request("POST", f"/v1/agents/{agent['id']}/tools", json=payload)
        after = await request("GET", f"/v1/agents/internal/agents/{source_id}/runtime-config")
        assert before == hashlib.sha256(after["instructions"].encode()).hexdigest(), "SAW instructions unexpectedly changed."
        return {"agent_id": agent["id"], "public_id": agent["public_id"], "name": agent["name"],
                "source_prompt_unchanged": True, "tool_names": [tool["name"] for tool in tools if tool["is_active"]]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--business-id", required=True)
    parser.add_argument("--source-agent-id", required=True)
    args = parser.parse_args()
    print(asyncio.run(provision(args.business_id, args.source_agent_id)))
