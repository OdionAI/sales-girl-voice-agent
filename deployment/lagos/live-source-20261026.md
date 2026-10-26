# Lagos voice source capture — 2026-10-26

The running `odion-recovery-lagos-voice-1` container on the private Lagos app
VM was healthy when inspected. Its image was
`attentive/voice:msgfix-20261026-1`, with immutable image ID
`sha256:58a45ab5bea01811ef54a718cefa31798ec48ddfdabb2c32d784a6b95ebdd4dd`.
The container was created at `2026-10-26T16:06:07Z` and still had the same
image ID after the source capture and local verification.

The image reports base source revision
`e56532b5d46bd84be28638c367c2b316b9316ecb` and overlay revision
`87b555f5218fae7cd10f7e6370b920beb5d9b706`. Those labels do not account
for every later layer, so the source was copied from `/app` in the running
container. The source archive, excluding the mounted trace artifacts and secret
files, has SHA-256
`73c7920cfb1c992f2036c156e0cecacf893812831f1d0ed76c98aad374c30a47`.

The imported runtime files are the live versions of `latency_lab/action_tools.py`,
`banking_tools.py`, `config.py`, `engagement.py`, `livekit_worker.py`,
`passkey_demo.py`, `pipeline.py`, and `providers.py`. The live worker's additional
`latency_lab/aicc_transfer.py`, `passkey.py`, regression tests, and
`sim_conversation.py` were also imported. The main branch's newer iOS SDK,
passkey diagnostics, and CI files were preserved. The banking regression test
assertions were updated to reflect the live prompt's background voice-check
policy; the application still performs both authorization checks.

This import began from GitHub `main` commit
`cd04eeaa5b75143b23e2c65830dc683e30533d61`. The node-B `deploy.yml`
workflow is a separate target and must not be used as evidence of a Lagos
release. Merging this source record does not recreate the Lagos container.
