# Native passkey demo worker overlay

This is an opt-in simulated bank, not Wema authorization. It verifies real native
registration and assertion proofs using py_webauthn, requiring the expected RP,
origin, challenge, ES256 credential, user presence/verification, credential owner
and assertion counter. Nothing in this module changes BankingTools authorization.
Credentials exist only in memory for the call. There is no money movement.

Only signed dispatch scopes listed in all three operator settings enable it:
`ATTENTIVE_PASSKEY_DEMO_BUSINESS_IDS`, `ATTENTIVE_PASSKEY_DEMO_AGENT_IDS` (internal
configuration IDs), and `ATTENTIVE_PASSKEY_DEMO_SUBJECTS`. Defaults are disabled.
The assigned SDK caller must send its private context before a request can run.
The operator-provisioned RP is `docs.odion.ai`.

The committed spoken/text request **Test my passkey** creates a demo credential
if needed, then authenticates it. No model decision can bypass final-transcript
intent validation. The model never receives challenges or proofs. Cancellation,
expiry, disconnect and generation cancellation do not approve an operation.
Six native prompts per call is the upper bound; a new call starts fresh.

Host requirements: SDK 0.1.4, code-level `passkeys` configuration for the RP,
Associated Domains entitlement, and an exact app ID in the RP's AASA file. A paid
Apple Developer team is required. Personal Team signing cannot run native
passkeys. Do not claim real-iPhone acceptance until a signed, associated app
completes both native sheets and the verifier accepts the assertion.

## Rollout

Use the current Lagos stack, preserving every existing Compose overlay, environment,
volume and on-box source change. Do not use the legacy Huawei deployment workflow.
Record the current voice image digest and source hashes, copy only the current
pipeline/worker files, apply the reviewed diff with a strict forward/reverse patch
check, and add `latency_lab/passkey_demo.py`. Build FROM the exact running image,
install `webauthn==2.7.1`, and require `pip check` plus the focused test suite.

Build and test before any restart. Refuse rollout if the live image changed or
there are active calls. Add an image/environment-only Compose override; keep the
full previous Compose file list. Initially keep the demo disabled until its host
app association is approved. Enable only the intended test workspace, agent and
caller. Preserve the previous image/Compose configuration for rollback, and check
health and worker registration after recreating only voice.

For rollback, remove the demo override and recreate only voice using the exact
previous Compose file list. Preserve databases, new call history and credentials.
