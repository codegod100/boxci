# boxci

Minimal Nix-flake CI engine with sleek-inspired pipeline YAML. Deployed at **https://ci.boxd.sh**.

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

## Remote (ci.boxd.sh)

```bash
curl https://ci.boxd.sh/health
curl https://ci.boxd.sh/api/pipelines
curl -X POST https://ci.boxd.sh/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"pipeline":"quick.yml"}'
curl https://ci.boxd.sh/api/runs
```

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
