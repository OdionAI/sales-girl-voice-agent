# Attentive Integration Architecture and Delivery Plan

Recorded: 2026-09-06. Working branch: `attentive-ios-sdk`.

Status: the product structure below is agreed in discussion. The optional iOS UI
extraction is implemented and verified locally; track delivery in [ROADMAP.md](ROADMAP.md).
Transport options are under evaluation; the gateway recommendation is not an implementation or
deployment approval. This note changes no running service or call behavior.

## Product Requirements

- Customers continue managing agents in the existing dashboard. This project
  concerns the caller experience, not a replacement dashboard API.
- Provide an API-only integration for customers building their own web, iOS or
  Android client, without requiring them to adopt Attentive's UI.
- Provide a headless SDK so customers can connect calls to their own UI.
- Offer the existing caller experience as a separate, optional UI library.
- Keep model orchestration, voice authentication, privileged-tool authorization,
  tool execution, turn decisions and recording policy on the backend.
- Expose call state, transcripts, tool activity, authentication status, audio
  energy and errors consistently so custom and bundled UIs have feature parity.
- Provide human documentation, a versioned API collection/schema, runnable
  examples and coding-agent integration instructions using the same contracts.
- Customer integration should use Attentive concepts, not require a LiveKit
  account, infrastructure setup or direct use of LiveKit APIs.

## Verified Current State

| Area | Exists now | Remaining work |
| --- | --- | --- |
| Call API | Dashboard `POST /api/public-agent/connection-details` returns RTC connection credentials | Customer-authenticated, versioned public contract; no raw-audio streaming endpoint exists yet |
| Voice enrollment | Separate dashboard `/api/public-agent/voice-enroll` GET and multipart POST; SDK enrollment flow | Harden identity binding and document lifecycle/security for external use |
| Swift core | `AttentiveVoice`, wrapping LiveKit 2.16.0; custom-UI call controls and events | Release packaging, compatibility guarantees, customer examples |
| Swift caller UI | Optional `AttentiveVoiceUI` product, consumed by `Examples/AttentiveSample`; unit and simulator UI/live-chat tests passed | Release packaging and physical-device/real-voice verification |
| Web | Existing public caller and dashboard integrations | Reusable headless web SDK and optional UI distribution |
| Android | No Attentive Android SDK | Later platform implementation and verification |
| Distribution | Local SwiftPM package at `sdks/ios` | Publish an installable package/repository and versioned releases; repository root is not currently a remote SwiftPM package |
| Documentation | [Existing API contract](call-api.md), Postman collection, [Swift README](../../sdks/ios/README.md), human/coding-agent integration guides, sample README and verification notes | Publish versioned guides and validate external installation |

Current HTTP call creation does not send or receive live PCM. The client's
realtime connection carries microphone and agent audio over WebRTC and signaling
over WebSocket. The normal Swift call interface does not require a LiveKit
import, but the package dependency is visible. The optional credential-provider
extension also exposes `serverUrl`, `roomName` and `participantToken`.

The current enrollment backend keys reference voiceprints by normalized email,
not customer ID. A customer ID alone does not authenticate a caller. Enrollment
status is not equivalent to passing either live voice check.

## What "Hide LiveKit" Means

There are separate requirements:

1. **No LiveKit integration work:** the customer calls our SDK and reads our docs.
   The wrapper can achieve this while retaining LiveKit internally.
2. **No LiveKit client dependency or public wire protocol:** even an API-only
   customer uses an Attentive protocol. This requires an Attentive transport
   boundary, such as a streaming gateway.
3. **No discoverable implementation detail:** do not promise this. Package
   inspection, notices or network characteristics can reveal infrastructure.
   Abstraction and access control matter; secrecy of a vendor name does not.

The previous statement that an API-only customer must use a LiveKit client was
conditional on the existing bootstrap-only API. It is not a product requirement
or a limitation preventing us from building an independent public protocol.

## Transport Options

### A. Attentive SDK Around the Existing WebRTC Transport

Customer code uses `AttentiveVoice` call controls and events. LiveKit remains an
internal dependency, with no customer-managed LiveKit account or server secrets.
This is the current Swift direction. Platform wrappers can preserve existing
media handling and avoid an additional media gateway hop.

This solves custom UI integration through our SDK, but does not satisfy an
API-only client that rejects all LiveKit client dependencies. Branding a domain
or renaming JSON fields does not change the underlying protocol. Preserve
third-party notices when distributing dependencies.

### B. Attentive WebSocket Audio Gateway

Expose a documented WSS protocol accepting audio frames and returning audio plus
call/transcript/auth/tool events. Customers use a standard WebSocket client and
their platform's audio APIs; no LiveKit client is required on their device.

An initial gateway could join an internal LiveKit room as the caller, publish
incoming audio and relay the agent's output and events. This is a proposed design
using LiveKit's documented server-side raw-audio publishing/subscription support,
not an existing gateway feature. See [raw media tracks](https://docs.livekit.io/transport/media/raw-tracks/).

This can serve both browsers and native apps. We must design and verify audio
formats, pacing, bounded queues, playback cancellation, reconnection and event
ordering. A WebSocket is not itself a microphone/playback engine. Reliable
ordered delivery can increase delay during network stalls; do not assume it
matches the WebRTC call experience without measurement.

### C. Attentive Bidirectional gRPC Audio Gateway

Define our own protobuf service for concurrent audio input/output and events.
Customers use generated service clients with a gRPC runtime, not LiveKit. Native
mobile and server integrations can use this route. gRPC supports independent
bidirectional message streams; it does not supply microphone capture or playback.
See [gRPC core concepts](https://grpc.io/docs/what-is-grpc/core-concepts/).

Options B and C can share an internal call bridge and event contract. Neither
protocol automatically solves echo cancellation, output underruns or latency.
The service would be ours, not LiveKit's room-management API relabeled as a
streaming audio API.

Do not promise the same native gRPC stream directly in every browser: the
official gRPC-Web client currently documents no client-side or bidirectional
streaming. Use a WebSocket or WebRTC browser path instead. See
[gRPC-Web streaming support](https://github.com/grpc/grpc-web#streaming-support).

### D. Attentive-Owned WebRTC Signaling and Media Edge

Offer our own signaling contract and terminate a standards-based WebRTC
connection at our media edge, then connect internally to the agent stack.
Browsers can use their WebRTC APIs; native apps still need a WebRTC engine/client
library, but not necessarily LiveKit. Signaling is application-defined; see
[WebRTC signaling](https://developer.mozilla.org/en-US/docs/Web/API/WebRTC_API/Signaling_and_video_calling).

This preserves a realtime-media-oriented client transport, but requires us to
operate or integrate negotiation, ICE/TURN, secure media, codecs, reconnects and
event translation. It is a larger project than wrapping the existing transport.
A plain reverse proxy of LiveKit signaling does not provide this independence.

## Recommendation Pending Transport Decision

Keep the agreed WebRTC-first SDK work. Separately, design an Attentive streaming
gateway for API-only customers: native gRPC and browser-compatible WebSocket
adapters over one internal bridge are a practical candidate. Do not quietly
switch existing calls or move the Swift SDK onto the new gateway.

```text
Customer's UI -> Attentive SDK -> existing WebRTC transport -> existing agent
               optional caller UI uses the same SDK

Customer's own audio client -> Attentive streaming API (proposed)
                              -> internal call bridge
                              -> internal LiveKit room -> existing agent
```

A bridge reuses the existing worker rather than immediately rewriting VAD,
turn-taking, voice auth and tools. It adds operational cost and may add latency
through queueing or codec conversion; benchmark against the current direct path.
Do not claim feature parity just because audio passes through it.

Direct customers would see an Attentive session identifier, scoped credentials,
our stream address and our event schema. Internal room credentials would stay on
the gateway. Paths, protobuf names and event names are not finalized here.

## Package Structure

Current local Swift package structure (not yet remotely published):

```text
AttentiveVoice       Headless Swift call and enrollment API
AttentiveVoiceUI     Optional SwiftUI caller views; depends on AttentiveVoice
AttentiveSample      Customer-style app consuming both libraries
```

Provide equivalent core/optional-UI separation for web and Android later.
Do not require customers using their own UI to import the UI product. Keep bank
branding, avatar, caller fields and bank-activity presentation configurable rather
than hardcoding Wema into a generic client. Keep existing Wema wire fields
compatible while any generalized API is versioned separately.

Core ownership includes call lifecycle, microphone permission/capture, playback,
mute, chat, reconnect state, event decoding and cleanup. Optional UI owns visual
state, forms, transcript, auth badges, tool-result display and audio-reactive
animation. The sample demonstrates integration, not a second implementation.

The core targets iOS 16; the UI library and sample target iOS 17. Core deployment
requirements are unchanged. Package versioning and backend compatibility must be
explicit.

## Backend and Client Responsibilities

The backend owns STT/LLM/TTS, agent VAD and turn-end decisions, interruption
decisions, both voice checks, privileged-tool execution and recording policy.
The device still owns permission, capture, audio routing, playback and appropriate
echo/noise handling. A raw-audio customer implements these; our SDK handles them
for SDK customers. Microphone audio must not wait for authentication to pass,
because those checks require the caller's speech.

A gateway must preserve caller identity/metadata, agent dispatch, continuous audio
delivery, voice-auth observation, data topics, chat, recording association and
disconnect cleanup. Interruption needs an explicit mechanism to discard queued
agent output on the client, not just stopping future generation on the server.
Never replay banking actions automatically after a reconnect or ambiguous error.

## External Release Security Requirements

- Bind tenant, agent and caller to a trusted customer session; validate profile
  ownership server-side. Never treat a supplied customer ID as authorization.
- Keep provider keys, LiveKit API secrets and internal service credentials off
  customer devices. Use short-lived, session-scoped client access over TLS.
- Retain both live voice checks and existing privileged-tool/confirmation gates.
  UI badges and client events do not authorize transactions.
- Limit call creation, session duration, payload sizes, audio queues and resource
  use. Define expiry, disconnect and reconnect behavior without duplicate calls.
- Sanitize public events/errors and restrict sensitive tool results to the correct
  caller. Do not log tokens, raw audio or financial payloads by default.
- Harden enrollment identity binding, replacement rules and voice-data retention.
  Do not overwrite real voiceprints in automated tests.

## Documentation Deliverables

Human documentation:

- Choose API-only, headless SDK or optional caller UI, with honest prerequisites.
- Versioned install steps, minimum OS/toolchain, microphone permissions, TLS and
  physical-device networking; no untested package URL or invented release tag.
- Runnable start/end/mute/chat examples and sample applications using the shipped
  APIs, including custom UI and bundled UI approaches.
- Call/enrollment lifecycle, transcript revisions, both auth checks, tool activity,
  errors, interruption, reconnect and privacy/recording behavior.
- OpenAPI/Postman for HTTP; protobuf and native examples if gRPC ships; a framed
  audio/event protocol and browser example if WebSocket ships. HTTP examples do
  not on their own demonstrate a working audio call.
- Troubleshooting and measured latency/reliability results, distinguishing live
  microphone tests from chat-only tests.

Coding-agent documentation:

- A copyable integration prompt plus versioned machine-readable instructions
  stating the exact package, version, supported APIs and integration steps.
- Shared schemas/examples with the human docs, preferably tested in CI rather
  than divergent snippets maintained separately.
- A checklist for permissions, scoped credentials, lifecycle cleanup, UI choice,
  physical-device validation and safe error handling.
- Explicit constraints: no embedded server secrets, no auth bypass, no provider
  fallback, no real transactions or replacement voice enrollment in smoke tests.

## Incremental Delivery and Acceptance

1. Extract the optional SwiftUI product without changing the working call path;
   make the sample consume it. Verify custom UI and bundled UI parity.
2. Publish a versioned Swift package with tested examples and human/agent docs.
   Coordinate API work on `attentive-api` and SDK work on `attentive-ios-sdk`;
   this note itself does not switch branches or publish either.
3. Decide the gateway transport and prototype it separately using harmless audio
   and conversation, then compare it against direct SDK calls.
4. Verify session/auth/tool/recording parity before offering it for banking calls.
   Add web and Android deliverables in separately scoped work.

For each transport, test permission grant/denial, actual microphone input,
intelligible agent playback, interruption and stale-audio flushing, chat,
transcripts, first voice-check failure/retry, both checks before privileged tools,
tool activity states, call recording association, network changes, expiry,
repeated calls and hangup while connecting. Use non-mutating or controlled test
tools. Measure first response, interruption-to-silence and queue growth rather
than equating connectivity with a successful call.

Existing intermittent DNS/STT disconnects and TTS stammering remain separate
runtime issues; packaging or a new transport is not evidence they are fixed.
See [current verification notes](../../sdks/ios/VERIFICATION.md).

No server/model settings, auth policy, audio thresholds, DNS or branch history
were changed to record this plan. Future infrastructure or transport changes
need a separately agreed scope and rollback/verification plan.
