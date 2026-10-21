# BGFI natural action confirmation — 2026-10-20

## Why this release was needed

The BGFIBank Bénin call showed an English application-owned ticket question
inside an otherwise French conversation. The caller then gave clear permission
to create the ticket, but the worker required one of a finite set of phrases
and repeated the question. The previous language hotfix translated the fixed
question and added phrases; this release removes the phrase dependency for
generic ticket and email actions.

## Behavior

- The configured agent asks a short confirmation question in its saved spoken
  language. The question is generated from the pending action and its details.
- A separate committed caller turn is still required before a ticket or email
  side effect. The model classifies the full reply as approval, refusal,
  revised request, or unclear; there is no list of required caller phrases.
- A revised request cancels the old pending action and returns to the normal
  agent turn. Unclear replies get a fresh question. Model failures never grant
  permission or execute the action.
- Wema transaction previews and the operator-scoped provider-directory email
  flow remain under their separate confirmation contracts.

## Release record

- Code commit: `6ddbc34aea5a23cef10089707a9baab579840c83`.
- Overlay commit: `7ce073ecce0e14b1e215faf20a594b2e2fec5fda`.
- VM source: `/opt/attentive/releases/recovery-20261009/sources/bgfi-natural-confirmation-6ddbc34`.
- Image: `attentive/voice:bgfi-natural-action-confirmation-20261020-1`.
- Compose overlay: `deployment/bgfi-natural-action-confirmation-20261020.override.yaml`
  within the VM source snapshot. All preceding overlays were retained.
- Only `odion-recovery-lagos-voice-1` was recreated. It started at
  `2026-10-20T17:18:26Z` and registered as `rvc-livekit-comparison`.

## Verification

- Local and release-image focused suites: 63 tests passed.
- Uploaded source checksums for action tools, pipeline, and overlay matched
  the local commit.
- A synthetic request through the live LLM produced a French ticket question.
  French and English natural approvals were `confirmed`; French refusal was
  `rejected`; a changed subject was `revised`; a question was `unclear`.
- The running worker reported no startup errors. A real caller test is still
  needed to assess the complete ASR → LLM → TTS flow and audible language.

## Rollback

From `/opt/attentive/releases/recovery-20261009`, run the same Compose file
chain shown in the worker's `com.docker.compose.project.config_files` label,
omitting only the `bgfi-natural-action-confirmation-20261020.override.yaml`
overlay, then recreate only `voice` without building. The prior image is
`attentive/voice:bgfi-french-action-confirmation-20261020-1`. Preserve the
earlier overlays and application data.
