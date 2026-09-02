#!/usr/bin/env bash
set -euo pipefail

endpoint="${QWEN_LLM_ENDPOINT:-http://102.88.137.124:8080/qwen38-standard/v1/chat/completions}"

curl --fail-with-body -sS "${endpoint}" \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.8_27b","messages":[{"role":"user","content":"Reply with one sentence confirming that you are reachable."}],"temperature":0,"max_tokens":256,"chat_template_kwargs":{"enable_thinking":true}}' \
  | jq -r '.choices[0].message.content'
