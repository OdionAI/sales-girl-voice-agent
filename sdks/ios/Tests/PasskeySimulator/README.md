# Internal simulated bank integration test

This Debug-only harness is excluded from the distributed SDK and customer sample.
It uses the real SDK call UI, media connection, private message channel and passkey
coordinator. A test credential device signs synthetic assertions with a fresh P256
key. The local bank verifies the signature, challenge, origin, RP hash and UV/UP
flags before returning a correlated result. No banking API is called and no money
moves. This is **not** proof of Face ID, hardware-backed key storage, attestation,
registration, Wema acceptance or a full conversational AI session.

From the repository root, install the test-only Python dependencies in a virtual
environment using `Tests/PasskeySimulator/requirements.txt` under this SDK folder.
With the local media server executable available, run these in separate terminals:

```sh
livekit-server --dev --bind 127.0.0.1 --node-ip 127.0.0.1 --rtc.enable_loopback_candidate
```

```sh
mkdir -p sdks/ios/.build
say -o sdks/ios/.build/pr17-greeting.aiff 'Hello. This is the simulated bank test assistant. This call is local, and no money will be moved.'
afconvert -f WAVE -d LEI16@24000 -c 1 sdks/ios/.build/pr17-greeting.aiff sdks/ios/.build/pr17-greeting.wav
python sdks/ios/Tests/PasskeySimulator/bank_server.py \
  --greeting sdks/ios/.build/pr17-greeting.wav \
  --evidence sdks/ios/.build/pr17-bank-evidence.jsonl
```

Both services listen only on loopback. The development media credentials are
fixed placeholders for this isolated test. Do not expose these services publicly.
Stop both processes when finished. Evidence includes only event names, timings
and validation outcomes; it excludes credentials and assertion payloads.

Generate the app:

```sh
node sdks/ios/script/create_quickstart_project.mjs sdks/ios --passkey-simulator
```

Open `.build/passkey-simulator-app/AttentiveQuickStart.xcodeproj` in this SDK
folder. Build the `AttentiveQuickStart` scheme for an iPhone simulator in Debug
with `ENABLE_TESTABILITY=YES` and `CODE_SIGNING_ALLOWED=NO`. The harness requires
source modules and `@testable` access; it cannot be used with the release package.
The generated harness alone permits plain HTTP for the loopback bootstrap.

1. Tap **Start call**. Confirm the agent speaks and the bank context is acknowledged.
2. Tap **Approve path**, then **Approve simulated passkey**. SDK action stays
   pending during bank verification, then becomes verified.
3. Tap **Reject path** and approve the synthetic prompt. SDK action becomes failed
   even though the assertion signature is valid.
4. Tap **Approve path**, then **Cancel simulated passkey**. No proof is sent and
   SDK action stays pending.
5. Tap **Native check**. This uses the real Apple authorizer. The test app has no
   association with `auth.bank.example`; failure is expected and must not authorize.
6. End the call. The SDK must reset the protected action to pending.

The readiness attribute is advertised only after the worker knows the caller,
so early packets cannot be mistaken for messages without an authenticated sender.
Real bank testing still requires approved relying parties, domain association,
bank challenge/verification endpoints and confirmation of the integration schema.
