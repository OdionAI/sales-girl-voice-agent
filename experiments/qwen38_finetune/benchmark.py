"""Read-only Chat Completions baseline for SalesGirl voice-model candidates.

This deliberately never executes a tool. Tool results in the fixture are synthetic.
Run the same cases and decoding settings against each provider, then have reviewers
score the saved replies for accuracy, naturalness, and spoken suitability.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def post_chat(url: str, api_key: str, payload: dict, timeout: float) -> tuple[dict, float]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    start = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.load(response)
    return body, round((time.perf_counter() - start) * 1000)


def tool_calls(message: dict) -> list[dict]:
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except (ValueError, TypeError):
            arguments = None
        calls.append({"name": function.get("name"), "arguments": arguments, "id": call.get("id")})
    return calls


def score(case: dict, message: dict, calls: list[dict]) -> dict:
    expected = case["expect"]
    wanted = expected.get("tool")
    choice = (len(calls) == 1 and calls[0]["name"] == wanted) if wanted else not calls
    arguments = True
    if wanted and choice:
        args = calls[0]["arguments"]
        schema = next(t["function"]["parameters"] for t in case.get("tools", [])
                      if t["function"]["name"] == wanted)
        arguments = isinstance(args, dict) and all(k in args and args[k] for k in schema.get("required", []))
        if arguments and schema.get("additionalProperties") is False:
            arguments = not (set(args) - set(schema.get("properties", {})))
    elif wanted:
        arguments = False
    spoken = (message.get("content") or "").strip()
    forbidden = [term for term in expected.get("forbidden", []) if term.lower() in spoken.lower()]
    return {"choice_correct": choice, "arguments_valid": arguments,
            "forbidden_terms": forbidden, "spoken_words": len(spoken.split())}


def synthetic_followup(case: dict, first: dict, call: dict) -> list[dict]:
    """Continue a tool exchange using a fixture response, without executing it."""
    call_id = call["id"] or "benchmark-call-1"
    assistant = {"role": "assistant", "content": first.get("content")}
    assistant["tool_calls"] = [{"id": call_id, "type": "function", "function": {
        "name": call["name"], "arguments": json.dumps(call["arguments"])}}]
    return case["messages"] + [assistant, {"role": "tool", "tool_call_id": call_id,
                                           "content": json.dumps(case["expect"]["tool_result"])}]


def request_payload(case: dict, model: str, provider: str, temperature: float) -> dict:
    payload = {"model": model, "messages": case["messages"], "temperature": temperature,
               "max_tokens": 180, "stream": False}
    if case.get("tools"):
        payload.update(tools=case["tools"], tool_choice="auto")
    if provider == "deepseek-api":
        payload["thinking"] = {"type": "disabled"}
        payload["reasoning_effort"] = "none"
    elif provider == "qwen-compatible":
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=ROOT / "cases.json")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--base-url", help="Base URL ending in /v1, or a full /chat/completions URL")
    parser.add_argument("--model")
    parser.add_argument("--api-key-env", default="MODEL_API_KEY")
    parser.add_argument("--provider", choices=["qwen-compatible", "deepseek-api", "generic"], default="generic")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--ids", help="Comma-separated case IDs for a connectivity smoke run")
    args = parser.parse_args()
    fixture = json.loads(args.cases.read_text())
    catalog = fixture["tools"]
    cases = fixture["cases"]
    if args.ids:
        selected = set(args.ids.split(","))
        cases = [case for case in cases if case["id"] in selected]
        if len(cases) != len(selected):
            parser.error("one or more --ids values were not found")
    for case in cases:
        case["tools"] = [catalog[name] for name in case.get("tool_names", [])]
        case["messages"] = [{"role": "system", "content": fixture["system"]}] + case["messages"]
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        parser.error("case IDs must be unique")
    for case in cases:
        assert case["messages"] and case["messages"][0]["role"] == "system", case["id"]
        assert case["expect"].get("tool") is None or case.get("tools"), case["id"]
    if args.validate:
        print(json.dumps({"valid_cases": len(cases), "slices": sorted({c["slice"] for c in cases})}))
        return
    if not (args.base_url and args.model and args.output):
        parser.error("--base-url, --model, and --output are required for a run")
    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        parser.error(f"{args.api_key_env} must be set")
    url = args.base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    results = []
    with args.output.open("w") as output:
        for case in cases:
            result = {"id": case["id"], "slice": case["slice"], "model": args.model}
            try:
                body, ms = post_chat(url, api_key, request_payload(case, args.model, args.provider,
                                                                     args.temperature), args.timeout)
                message = body["choices"][0]["message"]
                calls = tool_calls(message)
                result.update(response_ms=ms, text=message.get("content") or "", tool_calls=calls,
                              auto_score=score(case, message, calls), usage=body.get("usage"))
                if (case["expect"].get("tool_result") is not None
                        and result["auto_score"]["choice_correct"]
                        and result["auto_score"]["arguments_valid"]):
                    followup_case = dict(case, messages=synthetic_followup(case, message, calls[0]))
                    followup_body, followup_ms = post_chat(
                        url, api_key, request_payload(followup_case, args.model, args.provider,
                                                       args.temperature), args.timeout)
                    followup_message = followup_body["choices"][0]["message"]
                    followup_text = followup_message.get("content") or ""
                    expected = case["expect"]
                    result["followup"] = {
                        "text": followup_text, "response_ms": followup_ms,
                        "required_present": all(term.lower() in followup_text.lower()
                                                for term in expected.get("followup_required", [])),
                        "forbidden_terms": [term for term in expected.get("followup_forbidden", [])
                                            if term.lower() in followup_text.lower()],
                        "extra_tool_calls": tool_calls(followup_message),
                    }
            except (urllib.error.URLError, ValueError, KeyError, TimeoutError) as exc:
                result["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            output.write(json.dumps(result, ensure_ascii=False) + "\n")
            output.flush()
            results.append(result)
            print(f"{case['id']}: {'error' if 'error' in result else 'done'}")
    successful = [r for r in results if "auto_score" in r]
    print(json.dumps({"completed": len(successful), "total": len(cases),
                      "tool_choice_correct": sum(r["auto_score"]["choice_correct"] for r in successful),
                      "arguments_valid": sum(r["auto_score"]["arguments_valid"] for r in successful),
                      "output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
