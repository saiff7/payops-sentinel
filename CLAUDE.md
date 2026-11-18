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
| `src/payops_sentinel/ui/` | Streamlit entry (`app.py`, navigation) + Overview + 3 pages. Calls services only, never SQL. |
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
uv run payops investigate EXC-000004      # real agent run: Qwen3 + separate MCP process
uv run pytest -q                          # all tests (no Ollama needed; fake model)
uv run pytest -m ollama -q                # opt-in live tests against local qwen3:8b
uv run payops evaluate                    # 30 golden cases vs live model → evals/reports + DB
uv run payops eval-compare A.json B.json  # regression check (non-zero exit on regression)
uv run payops backup backups/x.db         # online backup + verification; payops verify-db
uv run ruff check . && uv run ruff format --check . && uv lock --check
uv run streamlit run src/payops_sentinel/ui/app.py      # UI (auto-creates DB + reference data)
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

## Key Phase 2 rules

- Ollama/Qwen3 never speaks MCP. `agent/orchestrator.py` bridges Ollama function calling to the
  MCP `Client`; the server runs as `python -m payops_sentinel.mcp_server.server --db --manifest`.
- Host enforcement (`agent/guardrails.py`): `ALLOWED_TOOLS` (must equal
  `governance/tool_permissions.toml`), Pydantic arg models with `extra="forbid"`, iteration/call/
  result-size limits from `config.toml [agent]`, repeated identical failure → stop.
- Host fetches ground truth (actor `host`) to verify drafts: evidence IDs must belong to the
  exception and have been returned to the model; `$` amounts and `EMP-` refs must appear in evidence.
- Two-stage model use: tool loop (`think=False`, no format) then schema-constrained draft (no tools),
  up to `max_draft_attempts`. Any failure → `manual_review`, run + tool events still recorded.
- Expected tool errors raise `ToolError` (message reaches model); crashes are masked by the SDK.
- Schema is v2; pre-release DBs are not migrated: `rm data/payops.db && uv run payops init`.

## Key Phase 3 rules

- Review actions only via `review/service.py` (`edit_draft`, `approve_draft`, `reject_draft`,
  `approved_artifact`). Each takes a `Principal`, draft id + version + content hash; stale,
  superseded, non-pending or hash-mismatched drafts raise `StaleDraftError`.
- Roles (`ROLE_PERMISSIONS`): agent=draft.create only; operator=upload+investigate;
  reviewer=investigate+edit/approve/reject/download; viewer=read. Denials are audited.
  Identity is simulated (`DEMO_PRINCIPALS`); production = OIDC + central RBAC.
- Edits create a new version (author=reviewer) and mark the old one `superseded`; approval approves
  the communication only (exception status unchanged); rejection → exception `manual_review`.
- Audit: `review/audit.append_event` must run inside a transaction; events for batch
  ingested/rejected, investigation completed, draft edited/approved/rejected, artifact downloaded,
  authorization denied. `verify_chain` detects altered and removed (non-tail) events.
- Restricted data (`redaction.py`): SSN/email/phone/card/routing patterns reject an upload
  (`RESTRICTED_CONTENT`) or a reviewer edit/approval.
- UI pages read via repositories and act only via services; `PAYOPS_CONFIG` overrides config path.

## Status

- **Phase 0 (complete):** MCP SDK v2.3.0 API verified; plan in `docs/BUILD_PLAN.md`.
- **Phase 1 (complete):** deterministic engine, SQLite schema (all tables incl. later phases),
  synthetic data generator, CLI, 92 tests passing, ruff clean.
- **Phase 2 (complete):** MCP server (6 read-only tools), guarded Qwen3 agent loop, structured
  drafts, fail-closed fallback, run/tool-event persistence, `payops investigate`. 144 tests + 2 live.
- **Phase 3 (complete):** Streamlit app (Batches, Exceptions, Governance), review service with
  role checks and versioned drafts, approved artifacts, hash-chained audit, restricted-content
  safeguards, telemetry, readiness gate. 184 tests passing (+2 live Ollama).
- **Phase 4 (complete):** 30-case evaluation harness (`operations/evaluations.py`, release gate,
  JSON + DB reports, regression compare), backup/verify-db, agent kill switch, CI workflow,
  docs (architecture, data dictionary, threat model, evaluation plan, runbook, handover, release
  checklist), README. 209 tests + 2 live.
- **Evaluation status:** qwen3:8b FAILS the gate narrowly: tool selection 0.571 (1.0.0) → 0.750
  (1.1.0) → 0.893 (1.2.0, task-scoped tools; 25/28). 0 security violations. Release blocked.
- **qwen3:14b** with the same prompt, tools, cases and thresholds **passes** (0.964, 27/28;
  `EVAL-20261117T235411Z`). Not adopted yet: config and manifest still name qwen3:8b, awaiting the
  maintainer's decision. The readiness gate uses only evaluations matching the configured model
  and prompt.
- **Prefetch experiment:** `[agent] prefetch` (default false). Host passes details + evidence to the
  model; runs labelled `host-prefetch` / prompt `+prefetch`; prefetched calls credited only there.
  qwen3:8b + prefetch passes (case pass 29/30, p95 51 s). Tool-selection accuracy is not comparable
  across methodologies; compare case pass rate, draft quality, safety, latency.
- **Release 0.6.0 (tagged `v0.6.0`, pushed to github.com/saiff7/payops-sentinel, CI green):** qwen3:8b + `prefetch = true` in config.toml;
  final validation 2 runs × 30/30 (EVAL-20261118T032820Z, …034429Z). Manifest: retrieval_mode
  host-prefetch, prompt `1.5.0+prefetch`, tools `1.2.0`, digest 500a1f067a9f (test enforces
  manifest == shipped config). Public repo; MIT licence.
- History: tools 1.2.0 `display_amounts` + prompt 1.3.0 fixed EXC-000003 cents; 1.4.0 honoured
  requested context tools; 1.5.0 prefetches the applicable plan rule.
- Task-scoped tools: `guardrails.scope_tools` hides `lookup_plan_rule` when no rule applies; calls to
  tools not offered are rejected (`REJECTED_NOT_OFFERED`). Package 0.5.0, prompt 1.2.0, tools 1.1.0.
- Rule: any prompt/tool/guardrail/model change needs a full `payops evaluate` + compare before release;
  never relax thresholds to make a run pass.
