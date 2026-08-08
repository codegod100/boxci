# boxci

Minimal Nix-flake CI engine with sleek-inspired pipeline YAML. Deployed at **https://boxci.boxd.sh**.

Repos carry their own **`.boxci`** pipeline config (like `.buildkite/pipeline.yml`). The boxci server clones the repo, loads `.boxci/pipeline.yml`, and runs it.

## `.boxci` format

Place a pipeline at **`.boxci/pipeline.yml`** (preferred) or **`.boxci.yml`** in the repo root:

```yaml
name: my-app          # optional display name
on:                   # optional trigger filter (omit = all triggers)
  - merge

env:
  RUST_BACKTRACE: "1"

steps:
  - label: ":test: Check"
    key: check
    if: build.env("BOXCI_TRIGGER") == "merge"
    depends_on: other-step        # optional
    allow_dependency_failure: true # optional
    command: |
      set -euo pipefail
      cd "${BOXCI_REPO_ROOT:?}"
      cargo test
```

### Triggers

| Trigger | Set by | Typical source |
|---------|--------|----------------|
| `merge` | `BOXCI_TRIGGER=merge` | Garden merge webhook, push to `main` |
| `issue` | `BOXCI_TRIGGER=issue` | Garden issue webhook, poll dispatch |
| `poll` | `BOXCI_TRIGGER=poll` | `POST /api/poll`, systemd timer (10m) |

When `on:` is present, the pipeline runs only if the incoming trigger matches.

Issue/poll runs also set `RADICLE_TRIGGER` and `RADICLE_ISSUE_ID` so repos can reuse `scripts/buildkite/*` unchanged (`BUILDKITE_COMMIT` / `BUILDKITE_BRANCH` are mirrored from the checkout tip).

### Server-provided env

| Variable | Description |
|----------|-------------|
| `BOXCI_TRIGGER` | Event name (`merge`, …) |
| `BOXCI_REPO_ROOT` | Checkout path on the boxci VM |
| `BOXCI_REPO_URL` | Clone URL used |
| `BOXCI_REPO_SLUG` | Stable slug (Radicle naked RID or repo name) |
| `BOXCI_ROOT` | boxci install root |
| `BOXCI_RUN_ID` | Run identifier |
| `GIT_SHA` | Commit being built |
| `GIT_BRANCH` | Branch name |

Supported `if` expressions (subset of Buildkite):

- `build.env("KEY") != "value"`
- `build.env("KEY") == "value"`
- `build.env("KEY") != null`

## Radicle Garden webhook

Per the [Garden CI & Webhooks guide](https://radicle.garden/help/ci), register **one
URL for all events** (dashboard *Send me everything*, or CLI with no event filter):

```
https://boxci.boxd.sh/api/webhooks/garden
```

boxci returns HTTP 200 for events it ignores (`{"ignored": true, "reason": "…"}`) so
Garden does not retry. Issue COB commits on `main` **auto-route** to the issue handler —
you do not need `/api/webhooks/garden/issue` as a second webhook.

### Garden delivery format

Each POST includes:

| Header | Value |
|--------|--------|
| `Content-Type` | `application/json` |
| `x-radicle-event-type` | `push`, `patch_created`, `patch_updated`, `branch_deleted`, … |
| `X-Hub-Signature-256` | `sha256=<hex>` — HMAC-SHA256 of the **raw** body (when a secret is configured) |

Push JSON body (from [CLI smoke test](https://radicle.garden/help/ci/cli)):

```json
{
  "after": "<40-char-sha>",
  "branch": "main",
  "context": "boxci",
  "commit_status_url": "https://nandi.radicle.garden/…/commit-status",
  "repository": {
    "id": "z9mjPzpVK472QXaaP1picc5U9xBR",
    "name": "sleek",
    "clone_url": "https://nandi.radicle.garden/z9mjPzpVK472QXaaP1picc5U9xBR.git",
    "http_url": "https://nandi.radicle.garden/z9mjPzpVK472QXaaP1picc5U9xBR",
    "default_branch": "main",
    "seeder": "<garden-node-nid>"
  },
  "commits": []
}
```

Patch events use `patch.id`, `patch.after`, `patch.target` instead of root `after`/`branch`
([Jenkins guide](https://radicle.garden/help/ci/jenkins)). boxci ignores them for merge CI.

Legacy broker JSON (`commit`, `repo`, `branch` at the root) still works for manual curls and
adapter scripts.

### What boxci does with each event

| `x-radicle-event-type` | Payload shape | boxci action |
|------------------------|---------------|--------------|
| `push` / `branch_updated` | `after` + `branch: main` + `repository` | **Merge build** (`on: merge`) |
| `push` on `main`, `after` is issue COB | same | **Issue agent** (`on: issue`) — auto-routed |
| `push` on non-`main` branch | `after` + `branch: issue/…` | **Ignored** |
| `patch_created` / `patch_updated` | `patch` + `repository` | **Ignored** |
| `branch_deleted` | `deleted: true` or delete payload | **Ignored** |
| Poll / schedule | — | `POST /api/poll` or systemd timer (not a Garden webhook) |

Branch names may include a namespace prefix (`<nid>/refs/heads/main`); boxci normalizes to
`main`.

### Register the webhook

**Dashboard (recommended)** — repo → Settings → Webhooks → Add webhook:

- **Payload URL:** `https://boxci.boxd.sh/api/webhooks/garden`
- **Integration name:** `boxci` (becomes `context` in the payload)
- **Content type:** `application/json`
- **Events:** *Send me everything* (or *Just the push event* if you only want merge CI)
- **Secret:** optional; if set, also configure `BOXCI_WEBHOOK_SECRET` on the boxci VM to
  the same value (boxci verifies `X-Hub-Signature-256`)

**CLI** — from a repo working copy ([manage webhooks CLI](https://radicle.garden/help/ci/cli)):

```bash
# Install rad-webhooks once:
curl -sSfL https://index.radicle.garden/raw/rad:z2jrMkSbYgoVVB2tnzDaja55iX42R/head/install.sh | bash

rad webhooks add \
  --name boxci \
  --nid <your-garden-node-nid> \
  --secret '<shared-secret>' \
  --url 'https://boxci.boxd.sh/api/webhooks/garden'

git add .radicle/webhooks/boxci.yaml
git commit -m 'Enable boxci webhook'
git push rad
```

The CLI writes an age-encrypted `.radicle/webhooks/boxci.yaml` (see Garden persistence docs
for git clean/smudge filters).

Set the same secret on the boxci VM:

```bash
boxd env set BOXCI_WEBHOOK_SECRET='<shared-secret>' --machine boxci
```

For manual `curl` tests when a secret is configured, send `X-Boxci-Secret: <secret>` instead
of re-signing the body.

If the webhooks adapter pipes to a script instead of a URL, use
`sleek/scripts/boxci/webhook-to-boxci.sh` (forwards broker or Garden JSON; pass
`X-Boxci-Secret` when `BOXCI_WEBHOOK_SECRET` is set).

### Issue → cursor-agent → patch

Mirrors Buildkite's `RADICLE_TRIGGER=issue|poll` flow:

| Endpoint | Purpose |
|----------|---------|
| `POST /api/webhooks/garden/issue` | New issue COB → run `on: issue` steps |
| `POST /api/poll` | Poll pending issues → run `on: poll` (lists COBs, dispatches agents) |
| `POST /api/webhooks/garden` | Merge builds; **auto-routes** issue COB commits to the issue handler |

Issue webhook payload:

```json
{
  "commit": "<40-char issue COB id>",
  "branch": "main",
  "repo": "rad:z9mjPzpVK472QXaaP1picc5U9xBR"
}
```

Poll (uses `BOXCI_DEFAULT_REPO_URL` when body omits `repo_url`):

```bash
curl -X POST https://boxci.boxd.sh/api/poll \
  -H 'Content-Type: application/json' \
  -d '{"repo":"rad:z9mjPzpVK472QXaaP1picc5U9xBR"}'
```

Dry-run agent prompt (no cursor-agent call):

```bash
curl -X POST https://boxci.boxd.sh/api/runs/from-repo \
  -H 'Content-Type: application/json' \
  -d '{"repo_url":"https://nandi.radicle.garden/z9mjPzpVK472QXaaP1picc5U9xBR.git","trigger":"issue","issue_id":"<id>","dry_run":true}'
```

**VM secrets** (via `boxd env set` or OpenBao → `/etc/profile.d/boxd-env.sh`):

- `CURSOR_API_KEY` — Cursor CLI
- `RADICLE_SECRET_KEY` — dedicated CI Radicle identity (OpenSSH PEM)
- `RADICLE_PUBLIC_KEY` / `RAD_PASSPHRASE` — optional

Install the 10m poll timer on the boxci VM:

```bash
sudo cp deploy/boxci-poll.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now boxci-poll.timer
```

### Persistent workspace (optional)

By default checkouts live under `$BOXCI_ROOT/workspaces/<slug>/`. Override per repo:

```bash
# boxci.service Environment=
BOXCI_WORKSPACE_z9mjPzpVK472QXaaP1picc5U9xBR=/home/boxd/sleek
```

## API

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Liveness |
| `POST /api/webhooks/garden` | Garden merge webhook → repo `.boxci` |
| `POST /api/webhooks/garden/issue` | Garden issue open → `on: issue` agent |
| `POST /api/poll` | Issue poll → `on: poll` |
| `POST /api/runs/from-repo` | Manual trigger with `repo_url`, `sha`, `trigger` |
| `POST /api/runs` | Legacy central pipelines in `pipelines/` |
| `GET /api/runs` | List recent runs |

Manual trigger (sleek):

```bash
curl -X POST https://boxci.boxd.sh/api/runs/from-repo \
  -H 'Content-Type: application/json' \
  -d '{
    "repo_url": "https://nandi.radicle.garden/z9mjPzpVK472QXaaP1picc5U9xBR.git",
    "repo": "rad:z9mjPzpVK472QXaaP1picc5U9xBR",
    "trigger": "merge",
    "sha": "<commit>",
    "branch": "main"
  }'
```

Or from the sleek repo: `./scripts/boxci/dispatch-merge.sh --sha <commit>`

## Local usage

```bash
nix develop
boxci run pipelines/quick.yml

# HTTP server
BOXCI_ROOT=$PWD nix run .#server
curl -X POST http://localhost:8080/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"pipeline":"quick.yml"}'
```

## Adding CI to a repo

1. Add `.boxci/pipeline.yml` with `on: [merge]` and your build steps.
2. Use `$BOXCI_REPO_ROOT` in commands (server sets this after checkout).
3. Register Garden webhook → `https://boxci.boxd.sh/api/webhooks/garden`.
4. (Optional) Set `NIXBUILD_TOKEN` or `OPENBAO_TOKEN` on the boxci VM for remote Nix builds.

**sleek** example: [`.boxci/pipeline.yml`](https://github.com/codegod100/sleek) — check, APK, Flatpak on merge.

## Project layout

| Path | Purpose |
|------|---------|
| `flake.nix` | Nix package + dev shell + apps |
| `runner/boxci/` | Pipeline runner, repo checkout, HTTP server |
| `pipelines/` | Legacy central pipelines (deprecated for repo-local `.boxci`) |
| `deploy/` | boxd deployment helpers |

## Deploy (boxd VM)

```bash
boxd machine cp -r /path/to/boxci boxci:/home/boxd/boxci
boxd machine exec boxci -- 'cd /home/boxd/boxci && bash deploy/vm-build.sh'
boxd machine exec boxci -- 'sudo systemctl restart boxci'
```

Ensure `NIXBUILD_TOKEN` or `OPENBAO_TOKEN` is set via `boxd env set` for artifact builds.
