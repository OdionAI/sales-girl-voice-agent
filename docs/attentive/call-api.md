# Attentive Call API: WebRTC First

For the agreed product structure, optional caller UI, documentation deliverables
and API-only transport options, see the
[integration architecture and delivery plan](integration-architecture.md).
That plan describes future options; the endpoints below remain the current
WebRTC bootstrap contract, not a raw-audio streaming API.

## Scope

This phase wraps the existing LiveKit call transport. It does not introduce a
gRPC audio gateway, change models, or move voice processing onto the device.
The customer continues configuring agents in the existing dashboard.

```
Customer iOS UI -> Attentive Swift SDK -> existing call-start HTTP API
                                      -> LiveKit signaling (WebSocket)
                                      -> LiveKit media (WebRTC)
                                             -> existing Python voice agent
                                             -> STT / LLM / TTS / tools / voice auth
```

The SDK owns microphone capture and playback through LiveKit. The Python agent
continues to own VAD, endpointing, interruption decisions, model orchestration,
voice checks, and tool execution. The client must not gate microphone publishing
on authentication success: the backend needs that audio to perform the checks.

`attentive-api` contains this integration contract and Postman collection.
`attentive-ios-sdk` builds on it with the wrapper. gRPC is deferred.

## Start a Call

`POST /api/public-agent/connection-details` on the dashboard host.
This is an existing endpoint, not a new route in the Python worker.

```json
{
  "businessSlug": "your-business",
  "agentPublicId": "agt_your_agent",
  "endUserContact": "caller@example.com",
  "language": "en",
  "wemaContext": {
    "customerId": "YOUR_CUSTOMER_ID",
    "accountNumber": "YOUR_ACCOUNT_NUMBER",
    "phoneNumber": "YOUR_PHONE_NUMBER"
  },
  "toolWaitSpeechMode": "tool_specific"
}
```

| Field | Meaning |
| --- | --- |
| `businessSlug`, `agentPublicId` | Existing public agent configuration. Required. |
| `endUserContact` | Caller email or international phone number. Required. Use the identity associated with voice enrollment when testing authentication. |
| `language` | Existing language code, e.g. `en`. |
| `wemaContext` | Optional Wema profile. Omit for other agents. These fields supply context, not proof of identity or authorization. |
| `toolWaitSpeechMode` | Optional `tool_specific` or `llm_generated`; preserves the existing backend behavior. |

The mobile wrapper does not expose model/provider overrides. It uses the current
server/dashboard configuration. Do not ship a model-provider key, LiveKit API
secret, or internal service token in an app.

Successful response (implementation details consumed inside the wrapper):

```json
{
  "serverUrl": "wss://rtc.example.com",
  "roomName": "generated-room-name",
  "participantToken": "SHORT_LIVED_PARTICIPANT_JWT",
  "participantName": "user"
}
```

The server may include extra fields. Do not log or persist the participant token.
The SDK joins the room, subscribes to agent audio, and publishes microphone audio.
Connection to the room and readiness of the agent are separate states.

Errors include HTTP 400 for invalid caller input, 402 for insufficient platform
airtime, and other HTTP errors for unavailable configuration or services. Display
a bounded error; do not automatically retry call creation (it can create another
room/session). Transport reconnection for an existing call is handled by LiveKit.

## In-Call Contract

| Operation/event | Existing transport |
| --- | --- |
| Microphone / agent audio | WebRTC audio tracks, not JSON/base64 chunks |
| Chat input | LiveKit text stream topic `lk.chat` |
| Transcripts | LiveKit transcription segments, keyed by participant and segment ID; later segments replace earlier revisions |
| Agent state | Participant kind `agent`, attribute `lk.agent.state` |
| Tool activity | Reliable data topic `odion.tool.activity`, with `event`, `call_id`, `tool_name`, `status`, and optional `arguments` / `result` |
| Session voice check | Data topic `odion.auth.status`: `status`, `authenticated` |
| Action voice check | Data topic `odion.auth.action_status`: `status`, `authenticated`, optional reason/score |
| Action outcome | Data topic `odion.auth.action` |
| Metrics | Data topic `odion.voice_lab.metrics` |

Authentication events are display-only. The backend remains the authority for
privileged tools. A client must never turn a badge into an authorization decision.
Tool events can contain sensitive banking data; do not send them to analytics or
persist them by default. They are observations, not transaction-execution APIs.

## Deployment and Security Boundary

- Use HTTPS for call creation and WSS for signaling, with publicly reachable
  LiveKit ICE/media addresses. A phone's `localhost` is not the developer's Mac.
- The Swift SDK requires an explicit development option to permit HTTP/WS. This
  is not a substitute for iOS ATS/local-network permissions.
- The existing public-caller endpoint is a POC bootstrap, not a completed
  customer-authenticated API. Before external production use, bind caller/agent
  access to a trusted customer session, validate profile ownership server-side,
  rate-limit call creation, and audit existing public profile-prefill routes.
- The SDK's credential-provider interface lets a customer backend supply scoped
  call credentials later without changing the audio API.
- Hiding LiveKit from the SDK's public types does not make it invisible in network
  traffic or package dependencies. This is an abstraction, not a security boundary.
- Enabling microphone use requires `NSMicrophoneUsageDescription`. Background
  calling requires a deliberate iOS audio/session and entitlement policy.

## Verification

Import `call-api.postman_collection.json`, set `base_url`, `business_slug`,
`agent_public_id`, and a test caller identity. The request starts a session, so run
it deliberately. No banking transaction endpoint is included in the collection.
Test normal conversation, interruption, transcript updates, both authentication
checks, and tool activity through the wrapper before releasing a mobile build.

Upstream references: [Swift SDK](https://github.com/livekit/client-sdk-swift),
[LiveKit transport](https://docs.livekit.io/transport/),
[agent turns](https://docs.livekit.io/agents/logic/turns/).
