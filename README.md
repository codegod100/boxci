# boxci

Minimal Nix-flake CI engine with sleek-inspired pipeline YAML. Deployed at **https://boxci.boxd.sh** (`ci.boxd.sh` is reserved globally on boxd — contact support to reclaim that name).

## Pipeline format

Inspired by [sleek](https://github.com/codegod100/sleek) `.buildkite/pipeline.yml`:

```yaml
env:
  BOXCI_TRIGGER: "default"

steps:
  - label: ":nix: Flake check"
    key: flake-check
    if: build.env("BOXCI_TRIGGER") != "poll"
    depends_on: other-step        # optional
    allow_dependency_failure: true # optional
    command: |
      set -euo pipefail
      nix flake check
```

Supported `if` expressions (subset of Buildkite):

- `build.env("KEY") != "value"`
- `build.env("KEY") == "value"`
- `build.env("KEY") != null`

## Local usage

```bash
nix develop
boxci run pipelines/quick.yml
boxci run pipelines/example.yml

# HTTP server
BOXCI_ROOT=$PWD nix run .#server
curl -X POST http://localhost:8080/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"pipeline":"quick.yml"}'
```

## Remote (boxci.boxd.sh)

```bash
curl https://boxci.boxd.sh/health
curl https://boxci.boxd.sh/api/pipelines
curl -X POST https://boxci.boxd.sh/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"pipeline":"quick.yml"}'
curl https://boxci.boxd.sh/api/runs
```

## Sleek merge builds (`pipelines/sleek-merge.yml`)

Builds sleek APK + Flatpak on merge to `main`, reusing sleek's `scripts/ci-nixbuild.sh`
and nix attrs `.#android` / `.#flatpak` (same as Buildkite/GHA).

**Trigger** (sets `BOXCI_TRIGGER=merge`):

- **Radicle merge (primary):** Buildkite step on `main` (Garden buildkite-adapter) calls
  `scripts/boxci/dispatch-merge.sh`, or Garden webhook → `scripts/boxci/webhook-to-boxci.sh`
- GitHub: sleek `.github/workflows/boxci-merge.yml` on push to `main` (mirror only)
- Manual: `./scripts/boxci/dispatch-merge.sh` from the sleek repo
- curl:

```bash
curl -X POST https://boxci.boxd.sh/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"pipeline":"sleek-merge.yml","env":{"BOXCI_TRIGGER":"merge","GIT_SHA":"<commit>"}}'
```

**VM setup** (boxd): clone sleek to `/home/boxd/sleek` (or override `SLEEK_ROOT`),
set `NIXBUILD_TOKEN` or `OPENBAO_TOKEN` in the boxci service environment.
Artifacts: `$BOXCI_ROOT/artifacts/sleek/<run-id>/`.

## Project layout

| Path | Purpose |
|------|---------|
| `flake.nix` | Nix package + dev shell + apps |
| `runner/boxci/` | Pipeline runner + HTTP server |
| `pipelines/` | Pipeline YAML definitions |
| `deploy/` | boxd deployment helpers |

## Sleek patterns reused

- Step keys + `depends_on` DAG
- Env-gated steps (`if:` with trigger env vars)
- Shell `command:` blocks with `set -euo pipefail`
- Nix flake for hermetic builds (`nix develop`, `nix build .#boxci`)
- Optional poll trigger step (like sleek's `RADICLE_TRIGGER=poll`)
