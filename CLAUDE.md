# PayOps Sentinel — Development Contract

Local-first, governed AI system that investigates **synthetic** payroll contribution
exceptions, retrieves evidence through a read-only MCP server, drafts employer-facing
communications, and requires human approval before exporting an artifact.

> Python decides **what** is wrong. The agent investigates **why**. A human decides
> what may leave the system.

Source of truth: the frozen blueprint (summarised in `docs/BUILD_PLAN.md`).

## Architecture (one app · one agent · one MCP server · one SQLite DB)

Streamlit UI → ingestion → deterministic reconciliation → SQLite
Agent orchestrator ⇄ Ollama (`qwen3:8b`, local) and ⇄ MCP client → **separate** MCP
server process (stdio, 6 read-only tools, read-only DB connection).
Review service (reviewer principal only) → versioned drafts → approvals → hash-chained audit.

## Folder responsibilities

| Path | Responsibility |
|---|---|
| `src/payops_sentinel/domain/` | Pydantic models, enums. No I/O. |
| `src/payops_sentinel/ingestion/` | CSV parse, validation, SHA-256 idempotent batch creation |
| `src/payops_sentinel/reconciliation/` | Deterministic rules (Decimal, cents, basis points). No AI imports. |
| `src/payops_sentinel/storage/` | `schema.sql`, connections (rw + read-only), repositories. All SQL lives here. |
| `src/payops_sentinel/mcp_server/` | MCP v2 `MCPServer`, six read-only tools (named `mcp_server` to avoid shadowing the `mcp` SDK) |
| `src/payops_sentinel/agent/` | Ollama client, orchestrator loop, prompts, output schemas, guardrails |
| `src/payops_sentinel/review/` | Authorisation, draft versions, approve/reject, audit chain, artifacts |
| `src/payops_sentinel/operations/` | Telemetry, asset registry, evaluations |
| `src/payops_sentinel/ui/` | Streamlit app + 3 pages. Calls services only, never SQL. |
| `governance/` | TOML manifests: agent asset, tool permissions, data classification |
| `data/synthetic/` | Generated synthetic fixtures (committed). `data/uploads`, `data/artifacts`, `*.db` are git-ignored. |
| `evals/`, `tests/` | Golden cases; unit/integration/mcp_server/agent/security/review/evals tests |

## Design boundaries (never violate)

1. Python 3.12, `uv` only. No new framework/service/DB without a documented need.
2. Money in integer cents; rates in basis points; `Decimal` + `ROUND_HALF_UP`; 1-cent tolerance.
3. The LLM never produces an authoritative amount. Reconciliation must run with Ollama off.
4. MCP tools are read-only, statically allowlisted host-side, argument-validated, and use a
   `mode=ro` + `PRAGMA query_only` connection. No write/approve/send/delete tool — ever.
5. Annotations (`read_only_hint`) are descriptive, not enforcement.
6. MCP stdout is protocol-only; logs go to stderr. Launch server with list args, minimal env.
7. Uploaded CSV is untrusted data, never instructions. Strict column allowlist; restricted
   columns rejected. Raw uploads are never sent to the model.
8. Agent output: Pydantic-validated `AgentInvestigationDraft`; every evidence ID must belong
   to the exception; `requires_human_review` is always true. Any failure → `MANUAL_REVIEW`.
9. Approval only via `review.service` with reviewer principal, draft id + version + hash.
   Drafts are versioned, never overwritten. Audit rows are append-only (triggers) and hash-chained.
10. No external LLM APIs, no `:cloud` models, no real personal data.
11. Every new business/security behaviour gets a regression test.

## Commands

```bash
uv sync                                   # install
uv run payops generate-data               # regenerate deterministic synthetic data + oracle
uv run payops init                        # create data/payops.db and load reference data
uv run payops ingest data/synthetic/payroll_with_exceptions.csv
uv run payops show BATCH-000001 --evidence
uv run pytest -q                          # all tests (no Ollama needed)
uv run ruff check . && uv run ruff format --check . && uv lock --check
uv run streamlit run src/payops_sentinel/ui/app.py      # Phase 3+
```

Done = focused tests → full suite → ruff check + format check all pass.
Synthetic data is generated, never hand-edited: change `synthetic.py`, then `payops generate-data`
(a test asserts the committed files are reproducible byte-for-byte).

## Key Phase 1 rules (see docs/BUILD_PLAN.md §3)

- Exception types: MISSING_CONTRIBUTION, INCORRECT_CONTRIBUTION_RATE (incl. contribution with no
  active enrollment → expected 0), CONTRIBUTION_AFTER_TERMINATION (period *starts* after
  termination), DUPLICATE_OR_INVALID_RECORD (one per invalid row, `reason_code` says why).
- Whole-file problems raise `FileRejectedError` and store nothing; row problems are stored as
  INVALID rows + `validation_errors` with only format-valid values kept.
- Idempotency key: (employer_id, file_sha256, ruleset_version). Ingestion is one transaction.
- IDs: `BATCH-000001`, `EXC-000001`, `EV-000001` from `id_sequences`.

## Status

- **Phase 0 (complete):** MCP SDK v2.3.0 API verified; plan in `docs/BUILD_PLAN.md`.
- **Phase 1 (complete):** deterministic engine, SQLite schema (all tables incl. later phases),
  synthetic data generator, CLI, 92 tests passing, ruff clean.
- **Next: Phase 2 — MCP server + agent** (awaiting explicit go-ahead). Needs Ollama + `qwen3:8b`.
