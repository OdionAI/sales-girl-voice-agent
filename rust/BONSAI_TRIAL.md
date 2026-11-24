# Scoped LLM Trial

This change is disabled by default. It selects the trial provider only after a
verified session matches both the configured business ID and an agent ID.
It does not select endpoints or credentials from participant data.

For the Odion AI trial, after the endpoint is verified and the release has
passed the normal dev, staging, and production gates, configure all of:

- `RVC_LAB_TRIAL_LLM_BUSINESS_ID=6ddb0d72-8bd7-4a66-b0bd-a1d2cddd4b9d`
- `RVC_LAB_TRIAL_LLM_AGENT_IDS=1330ef61-4341-430d-b83d-23f5780cbe0f,6826e3d5-4870-47a1-b98b-c4d4742bf391,24568766-e169-4ffd-bbbf-7d8160c6f89b,f3f7a4fc-e2ed-4ac2-bc4f-8320dc78e108,1d08fd00-1675-409c-8a52-c4debb891c72`
- `RVC_LAB_TRIAL_LLM_BASE_URL`: reachable OpenAI-compatible API base, including `/v1` if required.
- `RVC_LAB_TRIAL_LLM_MODEL`: exact model identifier advertised by that endpoint.
- `RVC_LAB_TRIAL_LLM_API_KEY`: secret from the approved secret store, never a URL or repository file.
- `RVC_LAB_TRIAL_LLM_FIRST_OUTPUT_TIMEOUT_SECONDS`: trial-only timeout; select
  between 0.2 and 120 seconds after measuring realistic prompts. The production
  default is 5 seconds; the HTTP client also has a 120-second total timeout.
- `RVC_LAB_TRIAL_LLM_EXCLUSIVE=true` on a dedicated companion worker rejects
  any signed job outside the tenant and agent allowlist before loading its
  agent configuration. Do not register that companion under the shared name.

The trial uses the existing `qwen` provider wire format, with thinking disabled.
The endpoint must support streaming chat completions, the worker's Qwen request
fields and tool calls before real calls are enabled. There is no automatic
fallback after a selected trial request fails.

Incomplete configuration rejects worker startup. To roll back, remove all
trial settings and redeploy via the normal release process; other calls
continue using their original provider. Do not change the default DeepSeek
configuration or the existing Qwen service. Validate account isolation, a
different-tenant negative case, real call audio and request tracing at each
promotion stage before enabling the settings in production.
