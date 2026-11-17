# Release checklist

A release may be marked **operationally ready** only when every item is ticked. The Governance
page shows the automated subset (the readiness gate in `operations/registry.py`).

## Automated

- [ ] CI green on the release commit (`.github/workflows/ci.yml`: lockfile, ruff, format, tests,
      synthetic-data reproducibility).
- [ ] `uv run pytest -m ollama -q` passes locally against the release model.
- [ ] `uv run payops evaluate` on the **full** 30-case dataset: release gate PASSED.
- [ ] `uv run payops eval-compare <previous>.json <candidate>.json`: no regressions, or each one
      reviewed and accepted in writing.
- [ ] `uv run payops verify-db` is HEALTHY on the demo database.

## Recorded in the release notes and `governance/agent_manifest.toml`

- [ ] Source commit / tag.
- [ ] `agent_version`, `prompt_version` (`agent/prompts.py`), `tools_version` (`mcp_server/tools.py`).
- [ ] Model name **and digest** (from the evaluation report `model_version`).
- [ ] Evaluation report file committed in `evals/reports/`; `evaluation_status` updated.

## Governance

- [ ] Tool permissions reviewed: `ALLOWED_TOOLS` == `governance/tool_permissions.toml`, all read-only.
- [ ] Technical owner named.
- [ ] Business owner named.
- [ ] **Backup owner assigned to a real, separate person** (never invented; `UNASSIGNED` blocks readiness).
- [ ] Runbook and recovery procedure reviewed; a backup restored and verified within the last 30 days.
- [ ] Threat model residual risks re-read; any new capability has a threat entry and a test.
- [ ] Known limitations updated in the README.
