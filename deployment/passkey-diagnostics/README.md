# Passkey diagnostics overlay

The live Lagos worker contains an independently deployed real-bank `PasskeyFlow`
that is not yet present on main. `worker.patch` adds diagnostics only to that
exact running source. Do not replace it with the simulated-bank worker on main.
The patch leaves transaction authorization and execution unchanged.

`baseline.json` pins the pre-patch SHA-256 of the two files. Refuse deployment if
the running image/source changed. Apply the patch to copies of the live source,
add `latency_lab/passkey_diagnostics.py`, and build FROM the exact running image.
Run the real worker's existing passkey tests plus the diagnostics tests in the
candidate image before restarting anything. Preserve all Compose overlays and
mounts. Refuse restart while calls are active. Recreate only voice, adding the
image override and `ATTENTIVE_PASSKEY_DIAGNOSTICS_ENABLED=1`. Retain the previous
image and complete Compose list for rollback.

The worker emits `passkey_diagnostic` into its Python log and existing call trace:
begin request/HTTP status, ceremony publication requested, context/receipt,
client failure reason, proof received, execution transport and cleanup. Publication
does not prove SDK receipt or native sheet display. Execution transport success
does not itself prove bank acceptance or money movement.

The helper accepts only fixed stage/reason/check labels, a known system-error
category and numeric code, and HTTP status. It never logs ceremony/proof contents,
device/customer identifiers, authentication IDs, domains or arbitrary error text.
Old SDK versions still report their existing safe failure reason. New SDK versions
can attach optional bounded stage/error metadata when diagnostics are enabled.
Disable the operator environment flag to stop these diagnostics.

SDK-side unified logs are separately opt-in through
`passkeys.diagnosticsEnabled`. They use subsystem `ai.odion.attentive`, category
`Passkeys`. This private operator document is not a customer transport API.
