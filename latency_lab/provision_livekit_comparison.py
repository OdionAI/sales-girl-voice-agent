"""Clone SAW's saved prompt and tools without modifying the existing agent."""

import argparse
import asyncio
import hashlib
import json

import aiohttp

from .config import lab_config_from_environ, load_platform_env

DESCRIPTION = "Isolated Wema comparison: RVC orchestration over LiveKit transport."


async def provision(business_id: str, source_id: str) -> dict:
    load_platform_env()
    config = lab_config_from_environ()
    headers = {"X-Service-Token": config.agent_config_service_token,
               "X-Business-ID": business_id, "X-Service-Name": "sales-girl-voice-agent"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async def request(method, path, **kwargs):
            async with session.request(method, config.agent_config_api_base_url + path, **kwargs) as response:
                response.raise_for_status()
                return await response.json()

        source = await request("GET", f"/v1/agents/internal/agents/{source_id}/runtime-config")
        source_tools = await request("GET", f"/v1/agents/{source_id}/tools")
        agents = await request("GET", "/v1/agents?size=200")
        matches = [a for a in agents["items"] if a.get("description") == DESCRIPTION]
        if len(matches) > 1:
            raise RuntimeError("Multiple comparison agents exist; refusing an ambiguous update.")
        if matches:
            agent = matches[0]
        else:
            payload = {"name": source["name"], "description": DESCRIPTION,
                       "instructions": source["instructions"], "theme_key": source.get("theme_key", "default")}
            for key in ("tts_provider", "tts_voice_id", "tts_owner_id", "tts_language_scope", "tts_language_hint", "knowledge_base_ids"):
                if source.get(key) is not None:
                    payload[key] = source[key]
            agent = await request("POST", "/v1/agents", json=payload)
        existing = await request("GET", f"/v1/agents/{agent['id']}/tools")
        fields = ("name", "description", "kind", "method", "url", "headers", "request_schema", "is_active")
        by_name = {tool["name"]: tool for tool in existing}
        for tool in source_tools:
            if not tool["is_active"]:
                continue
            payload = {key: tool[key] for key in fields}
            if tool["name"] not in by_name:
                await request("POST", f"/v1/agents/{agent['id']}/tools", json=payload)
            elif any(by_name[tool["name"]].get(key) != value for key, value in payload.items()):
                raise RuntimeError("Comparison tool configuration differs; inspect before updating.")
        cloned = await request("GET", f"/v1/agents/internal/agents/{agent['id']}/runtime-config")
        after = await request("GET", f"/v1/agents/internal/agents/{source_id}/runtime-config")
        assert source["instructions"] == after["instructions"] == cloned["instructions"], "Prompt mismatch"
        assert source_tools == await request("GET", f"/v1/agents/{source_id}/tools"), "Source tools changed"
        return {"agent_id": agent["id"], "public_id": agent["public_id"], "name": agent["name"],
                "prompt_sha256": hashlib.sha256(source["instructions"].encode()).hexdigest(),
                "tool_names": [t["name"] for t in source_tools if t["is_active"]]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--business-id", required=True)
    parser.add_argument("--source-agent-id", required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(provision(args.business_id, args.source_agent_id)), indent=2))
