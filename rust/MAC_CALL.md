# Local Mac call

The demo is currently stopped for review after an ASR engine failure during the interactive test. See [ASR_REVIEW.md](ASR_REVIEW.md) for the failure evidence, local stream guards, and offline validation. The updated worker has not been retested against production providers.

Open http://localhost:17900 in Chrome, click **Start call**, and allow microphone access. Helen will use the existing node1 ASR/TTS and DeepSeek API through the native Rust coordinator. You can speak over a reply to test interruption. **End call** disconnects the microphone and retires only this local room. Start again for another call.

The RTC server, caller API and worker run on this Mac. No production worker is registered, deployed, restarted or reconfigured. This is the generic English prototype, with the integration limits documented in `RVC_REVIEW.md`. Provider keys stay in a mode0600 file outside Git and are never sent to the browser. Interactive traces remain local under the ignored `artifacts/mac-live-call/` directory.

The official browser SDK is pinned to LiveKit2.23.0; its npm SHA512 integrity is checked before serving the local asset. The caller API binds to127.0.0.1:17900, accepts mutations only from localhost origins, and issues microphone-only, room-scoped local tokens. It cannot select a production RTC URL or dispatch agent.

## Start again

```sh
cargo build --locked --release --features local-call --bin rvc-worker --bin rvc-local-call
scripts/start-mac-call.sh /absolute/private-config.json
```

Native builds use the official SDK archive as described in `README.md`. Keep the existing ASR/TTS SSH forward on127.0.0.1:18080 available; DeepSeek uses its configured HTTPS endpoint. The startup script refuses occupied ports and stops only its owned local processes when terminated. The worker supervisor re-registers after a call ends.

The running launcher PID is recorded in `artifacts/mac-live-call/pids.txt`. Terminating that launcher stops its local RTC server, worker supervisor and browser API. It does not alter the model services.

## Verification

A normal call and an interruption call completed locally against the existing real ASR, DeepSeek and TTS services. Synthetic test input/receiver audio and worker traces are in `artifacts/rvc-mac-production-model-smoke/`. The browser page and official SDK exports were checked, and the local token/create/end API, origin rejection and scoped grants passed. The microphone remains for the user to enable through **Start call**.

44 core Rust tests and the new local-origin test passed, together with strict all-feature/all-target clippy and formatting. No production credentials are included in browser assets or test artifacts.
