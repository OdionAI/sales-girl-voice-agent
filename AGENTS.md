# Agent workflow for sales-girl-voice-agent

## Before editing

1. Inspect `git remote -v`, then `git fetch --prune`.
2. Start from the latest remote `dev` and create a task branch. Do not work on a
   stale checkout or push implementation commits directly to shared branches.
3. Before pushing the task branch, fetch again and incorporate the latest
   `dev`. Never force-push `dev`, `staging`, or `main`.

## Promotion and deployment

- Merge reviewed work through `dev` -> `staging` -> `main`.
- `dev` and `staging` run CI; they do not deploy the active Lagos worker.
- `.github/workflows/deploy-lagos.yml` deploys `main` to the Lagos `voice`
  service only when `LAGOS_VOICE_AUTO_DEPLOY=true` is configured in GitHub
  Actions. `[skip deploy]` in a commit message suppresses that automatic run.
- Manual runs default to a read-only verification. Keep the enable variable off
  until the release is intentionally authorized and GitHub SSH credentials are
  configured.
- `.github/workflows/deploy.yml` is for the older node-B workers and is manual
  only. Do not deploy directly from a VM or use a repo-local release command.

Read `deployment/lagos/README.md` and the workspace
`SALES_GIRL_PLATFORM_STATUS.md` before changing runtime or deployment behavior.
