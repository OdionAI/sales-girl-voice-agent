# Banking and passkey journey diagnostics

The native RVC worker writes a JSONL trace per call under
`/app/latency_lab/artifacts/traces/<session-id>.jsonl`. Events include a
`journey_id` (the session ID), a bank `request_id`, stage, timestamp/elapsed
time, and outcome. The worker sends the bank request ID in `X-Request-Id` so
banking-service logs can use the same correlation key.

## Expected event order

1. `tool_started` — the model-selected bank tool entered dispatch.
   `tool_confirmation_requested` and `tool_confirmation_decision` carry the
   same `request_id` when the action requires caller confirmation.
2. `banking_preflight_failed` — local checks rejected the call before HTTP.
   `stage` and `reason_code` identify tool configuration, caller profile,
   argument validation, confirmation state, scope headers, or voice auth.
3. `banking_authorization_result` — voice authorization is verified, failed,
   or not required by the signed session policy. When checked, the event carries
   match result, score, threshold, and sidecar reason code.
4. `banking_http_request_started` — method, destination host/path, request
   field names, safe request values, configured timeout, and scoped header
   names. Sensitive values are represented as `[withheld: sensitive field]`.
5. One of `banking_http_transport_failed`, `banking_http_response_read_failed`,
   or `banking_http_response_received` — transport reason, status code, elapsed
   time, response size, allowlisted response headers, and safe parsed response
   body.
6. `tool_finished` — final status, safe message, result keys, and auth status.
7. For device approval, `passkey_ceremony_started`, `passkey_app_message`,
   and `passkey_outcome` show the app handoff, bounded client failure reason,
   correlation checks, and final outcome. Device API HTTP requests and
   responses use the same `banking_http_*` event family.

If the bank tool batch is rejected before a `tool_started` event, look for
`banking_batch_rejected`. It records whether the banking tool was offered,
speech was enabled, how many calls were present, requested tool names, and the
configured banking tool names and request IDs.

## Failure isolation

- No `banking_http_request_started`: inspect the most recent preflight or batch
  rejection event. The request did not reach the banking endpoint.
- Request started, then transport failure: inspect connection/timeout flags
  and the matching request ID in voice-worker and banking-service logs.
- HTTP response received with a non-success status: inspect the status and safe
  response body in the trace, then correlate `request_id` with the bank log.
- HTTP success but `tool_finished` failed: inspect the returned `status`,
  `message`, and result shape; the response may be malformed or not a prepared
  operation.
- Passkey app failure: inspect `failure_reason_code` and the two match flags.
  A mismatch or missing app message identifies a different ceremony stage.

## Example trace excerpt

```json
{"event":"banking_http_request_started","data":{"journey_id":"session-abc","request_id":"tool-call-123","stage":"banking_http","method":"POST","url":"banking.internal/v1/tools/wema_prepare_airtime_purchase","request_fields":["amount","network","phone_number"],"request_body":{"amount":"500","network":"MTN","phone_number":"[withheld: sensitive field]"}}}
{"event":"banking_http_response_received","data":{"journey_id":"session-abc","request_id":"tool-call-123","stage":"banking_http_response","http_status":400,"success":false,"response_body":{"status":"failed","code":"VALIDATION_ERROR","message":"The bank rejected the request."}}}
{"event":"tool_finished","data":{"journey_id":"session-abc","request_id":"tool-call-123","tool_name":"wema_prepare_airtime_purchase","stage":"tool_result","status":"failed"}}
```

The excerpt makes the failed boundary explicit: the request reached the bank,
which returned HTTP 400 with `VALIDATION_ERROR`. If the request instead failed
voice authorization, there would be a `banking_preflight_failed` event and no
`banking_http_request_started` event.

## Data handling

Diagnostics intentionally withhold customer/account IDs, phone numbers, email,
access tokens, credentials, authorization headers, passkey proof/assertion
material, audio, and signatures. Other request/response fields remain visible
when safe. Do not enable raw proof or token logging: those values can authorize
or replay an account action. Share the resulting JSONL trace and matching bank
log records with Wema through the team's approved support channel.
