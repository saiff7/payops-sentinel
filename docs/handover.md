# Handover

The goal: someone who did not build PayOps Sentinel can operate and maintain it without reading the
source code. Start with `README.md`, then this page, then `docs/runbook.md`.

## For a business operator

| Task | How |
|---|---|
| Upload a batch | **Batches** page → choose identity *Demo operator* → upload a CSV (or use a demo button). A rejected file shows its reason; nothing is stored. Uploading the same file again returns the existing batch. |
| Understand an exception | **Exceptions** page. `MISSING_CONTRIBUTION`: blank or zero despite an active election. `INCORRECT_CONTRIBUTION_RATE`: differs from the configured calculation by more than 1 cent, or a contribution with no active enrollment. `CONTRIBUTION_AFTER_TERMINATION`: the pay period starts after the termination date (flag only). `DUPLICATE_OR_INVALID_RECORD`: a repeated row or invalid values (see `reason`). |
| Inspect evidence | Exceptions page → *Evidence* expander. Each record has a source reference and a SHA-256. |
| Get an AI draft | *Run AI investigation* (30–60 s). The trace shows every tool the model chose. If it says *manual review*, investigate from the evidence; nothing is lost. |
| Review a draft | Identity *Demo reviewer* → **Edit** (creates a new version), **Approve for export**, or **Reject** (reason required). Approval covers the communication only, not the payroll exception. |
| Download the artifact | Appears after approval. It is a text file; the system never sends email. Downloads are audited. |
| Something fails | Follow `docs/runbook.md`; contact the owners in `governance/agent_manifest.toml`. |

## For a technical operator

| Question | Answer |
|---|---|
| Which model? | `qwen3:8b` (digest `500a1f067a9f`) via local Ollama (`config.toml [agent]`). The exact digest is in every `agent_runs.model_version` and evaluation report. |
| Which retrieval mode? | `host-prefetch` (`[agent] prefetch = true`): the host supplies details, evidence and the applicable plan rule; the model chooses further reads (e.g. a requested batch summary). Runs record prompt `1.5.0+prefetch`. |
| How does the MCP process start? | Per investigation, the host runs `python -m payops_sentinel.mcp_server.server --db <db> --manifest <toml>` over stdio (`agent/orchestrator.py::server_parameters`). |
| What can its tools access? | Six read-only tools (`governance/tool_permissions.toml`) over a `mode=ro` + `query_only` SQLite connection. No write, approve, send or delete tool exists. |
| How is agent configuration versioned? | `PROMPT_VERSION` (`agent/prompts.py`), `TOOLS_VERSION` (`mcp_server/tools.py`), `agent_version` (package version), the model digest, and `governance/agent_manifest.toml`, all recorded per run and per evaluation. |
| How do I run the evaluation? | `uv run payops evaluate` (needs Ollama). It writes `evals/reports/EVAL-*.json` and DB rows, and prints the release gate result and a comparison with the previous report. CI runs the harness with a scripted reference model. |
| How do I restore the database? | `docs/runbook.md` → *Backup and restore*. |
| How do I disable AI investigation? | `config.toml [agent] enabled = false`, then restart the UI. |
| How do I roll back an agent release? | `docs/runbook.md` → *Roll back an agent release*. |
| Where are the limits? | `config.toml [agent]`: 4 turns, 8 tool calls, 10,000-character results, 15 s tool timeout, 2 draft attempts. |
| How do I add a tool? | Don't, without: a documented need, a read-only implementation, an allowlist plus manifest entry, an argument model, a threat-model entry, tests and new evaluation cases. Write tools are out of scope by design. |

## Ownership

Recorded in `governance/agent_manifest.toml`. The backup owner is deliberately `UNASSIGNED` until a
real second person accepts responsibility, so the asset stays *not operationally ready* (shown on
the Governance page). This is intentional governance, not an omission.

## Development contract

`CLAUDE.md` lists the non-negotiable design boundaries and the commands. Every change runs
`uv run pytest -q`, `uv run ruff check .` and `uv run ruff format --check .`. Any change to prompts,
tools or the model requires a full `payops evaluate` and a comparison before release.
