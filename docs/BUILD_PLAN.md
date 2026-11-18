# PayOps Sentinel — Build Plan (Phase 0 output)

Date: 2026-11-17 · Status: Phase 4 complete (208 tests + 2 live Ollama tests). All four phases delivered.

## 1. Repository and environment assessment

| Item | Finding | Impact |
|---|---|---|
| Repository | Empty directory, **not a git repo** | Run `git init` before Phase 1 so each phase is a reviewable commit; required for GitHub Actions (Phase 4). |
| Hardware | Apple M1 Max, **64 GB** RAM, macOS 15.7.3 | More headroom than the blueprint's 32 GB assumption; `qwen3:8b` is comfortable, 14B comparison is feasible. |
| Python | 3.12.14 (Homebrew) available; uv 0.9.26 installed | Meets MCP SDK (≥3.10) and Streamlit (≥3.11). Pin `.python-version` = 3.12. |
| Ollama | **Not installed** | Not needed for Phase 1. Install the macOS app (needs macOS ≥14 — OK) and `ollama pull qwen3:8b` before Phase 2. |
| Project path | Contains a space (`PayOps Sentinel`) | Harmless if subprocesses use list args (no shell). Enforced by design boundary 6. |

Current PyPI versions (checked 2026-11-17): `mcp 2.3.0`, `ollama 0.6.3`, `streamlit 1.67.0`, `pydantic 2.14.3`.

## 2. MCP SDK v2 — verified API (not from tutorials)

Installed `mcp 2.3.0` in a scratch venv and ran a probe server through **both** an
in-process client and a real stdio subprocess. Confirmed:

```python
from mcp.server import MCPServer                 # (FastMCP is renamed; do not use mcp.server.fastmcp)
from mcp.types import ToolAnnotations            # fields: read_only_hint, open_world_hint, ... (snake_case)
from mcp import Client, StdioServerParameters

server = MCPServer("payops-sentinel")
@server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def get_batch_summary(batch_id: str) -> BatchSummary: ...   # Pydantic return → structured_content
server.run("stdio")

async with Client(server) as c:                                   # tests: in-process
async with Client(StdioServerParameters(command=sys.executable,   # app: separate process
        args=["-m", "payops_sentinel.mcp_server.server"], env={...}, cwd=...)) as c:
    (await c.list_tools()).tools        # .name, .input_schema, .annotations
    r = await c.call_tool(name, args)   # r.is_error, r.structured_content, r.content
```

Unknown tools return `is_error=True` (server-side), but the **host allowlist rejects them before any call**.
Dependency: use plain `mcp>=2,<3` — the `[cli]` extra is a dev convenience not needed at runtime.

## 3. Technical problems found and decisions taken

1. **`mcp` name shadowing.** The blueprint's `src/payops_sentinel/mcp/` and `tests/mcp/` would
   shadow the SDK's `mcp` package (running `server.py` as a script, or pytest's default `prepend`
   import mode with `tests/` on `sys.path`). **Decision:** `payops_sentinel/mcp_server/`,
   `tests/mcp_server/`, launch via `python -m`, and pytest `--import-mode=importlib`.
2. **Three vs four exception types.** The task brief says "three main discrepancy types"; the blueprint
   specifies four. **Decision:** implement all four (superset):
   `MISSING_CONTRIBUTION`, `INCORRECT_CONTRIBUTION_RATE`, `CONTRIBUTION_AFTER_TERMINATION`,
   `DUPLICATE_OR_INVALID_RECORD` (with a `reason_code`).
3. **Business-rule ambiguities resolved** (documented, tested, configurable where noted):
   - Expected = `ROUND_HALF_UP(eligible_comp_cents × rate_bp / 10000)`; mismatch only if `|actual − expected| > 1` cent.
   - *Missing*: row exists, active enrollment on `pay_period_end`, comp > 0, election > 0, contribution blank/0.
     An absent payroll row is **not** an exception.
   - *Incorrect rate*: contribution > 0 and outside tolerance.
   - *After termination*: `pay_period_start > termination_date` and contribution > 0 (a final paycheck covering
     the termination date is normal). Flag for investigation only.
   - *Duplicate/invalid*: repeated `(employee_ref, pay_period_end)` in a batch (2nd+ occurrence), negative/malformed
     amounts, bad dates, malformed alias, alias unknown to the participant register. Invalid rows are stored with
     `validation_status=INVALID` and are not reconciled further.
   - The CSV allowlist has no employer column → employer is selected at upload (single synthetic employer: Northstar Manufacturing).
4. **Qwen3 thinking + structured output.** Qwen3 emits reasoning by default and Ollama does not reliably combine
   `tools` and `format` in one call. **Decision:** two stages — (a) tool loop with `think=False`, `temperature=0`,
   no `format`; (b) a final call with `format=AgentInvestigationDraft.model_json_schema()` and no tools, then
   independent Pydantic + evidence-ownership validation.
5. **Async MCP client inside sync Streamlit.** Run each investigation via `anyio.run(...)` in a service function;
   one stdio server process per investigation (simple, isolated, no long-lived process to manage).
6. **SQLite read-only access for MCP.** `file:<db>?mode=ro` URI + `PRAGMA query_only=ON`. Keep the default rollback
   journal (not WAL) so read-only readers never need to create `-shm` files; set `busy_timeout`.
7. **Audit tamper tests.** Append-only triggers block UPDATE/DELETE, so the tamper-detection test must drop the
   trigger in a test DB first — this demonstrates "tamper-evident, not immutable" honestly.
8. **Reviewer identity.** Local demo uses a simulated principal selector (`reviewer` / `agent_service` / `viewer`)
   with server-side role checks. OIDC (`st.login`) is documented as the production path, not claimed.
9. **Prompt injection via CSV.** All allowlisted fields are strictly typed (alias regex, ISO dates, decimal money), so
   no free text from uploads can reach the model. Tool results are wrapped as data in the prompt.
10. **Backup owner.** Manifest records `backup_owner = "UNASSIGNED"` and `release_status = "demo-not-production-ready"`
    until a real person is named. No invented identities.

## 4. Phase plan

Each phase ends with: focused tests → full suite → ruff check/format → CLAUDE.md status update → stop.

### Phase 1 — Deterministic business engine (no AI)
Files: `pyproject.toml`, `.python-version`, `.gitignore`, `config.toml`, `src/payops_sentinel/{config.py,
domain/{models,enums}.py, ingestion/{csv_loader,validator,service}.py, reconciliation/{engine,rules}.py,
storage/{db.py,schema.sql,repositories.py}}`, `scripts/generate_synthetic_data.py` (seeded, deterministic),
`data/synthetic/{participants.csv,plan_rules.json,payroll_clean.csv,payroll_with_exceptions.csv}`,
`evals/expected_reconciliation.json`, `tests/{conftest.py, unit/test_validation.py, unit/test_reconciliation.py,
unit/test_idempotency.py, integration/test_pipeline.py}`, plus a tiny CLI `python -m payops_sentinel.cli ingest <csv>`
for the demo.
Schema: full table set from blueprint §7 created now (incl. audit triggers) so later phases only add code.
Tests: clean file, missing columns, restricted columns, empty file, malformed dates/money, duplicates, each of the four
exception types, inactive enrollment, effective-date boundary, 1-cent rounding, same file twice (idempotent),
transaction rollback, golden file vs `expected_reconciliation.json`.
Demo: ingest `payroll_with_exceptions.csv` with Ollama absent; print exceptions + evidence from SQLite.

### Phase 2 — MCP server and real agent
`mcp_server/{server,tools}.py` (6 tools, Pydantic I/O, bounded `limit` 1–25), `agent/{ollama_client,orchestrator,
prompts,schemas,guardrails}.py`, `governance/{tool_permissions,agent_manifest}.toml`, `agent_runs`/`tool_events`
persistence. Limits: 4 iterations, 8 calls, 10 000-char results, stop on repeated failed call, timeout → manual review.
Tests: in-process client per tool, one real stdio test, fake-model orchestrator tests (allowlist, bad args, limits,
Ollama down, invalid JSON, foreign evidence IDs), one opt-in `@pytest.mark.ollama` live test.

### Phase 3 — Review application and governance
Streamlit `app.py` + `1_Batches`, `2_Exceptions`, `3_Governance`; `review/{service,audit,artifacts}.py`;
`operations/{telemetry,registry}.py`; `governance/data_classification.toml`.
Tests: agent cannot approve, wrong role rejected, superseded version rejected, hash mismatch rejected,
audit chain verifies / detects tampering, restricted-column and content-pattern rejection.

### Phase 4 — Evaluation, CI and handover
`operations/evaluations.py` (30 golden cases, acceptable tool paths, JSON report + DB rows, run-to-run comparison),
`tests/evals/`, `.github/workflows/ci.yml` (uv, pytest excluding `ollama` marker, ruff, `uv lock --check`),
`docs/{architecture,data_dictionary,threat_model,evaluation_plan,runbook,handover,release_checklist}.md`, README.
Real-model evaluation runs locally only; reported numbers come from actual runs.

## 5. Dependencies (minimal)

Runtime: `mcp>=2,<3`, `ollama`, `streamlit`, `pydantic>=2,<3`. (`anyio` arrives with `mcp`.)
Dev: `pytest`, `ruff`. Nothing else unless a later phase documents the need.

## 6. Phase 1 deviations from the blueprint (deliberate)

- Added `data/synthetic/employer.json` and `enrollments.csv` (effective-dated enrollments need
  their own file); `validation_errors` and `id_sequences` tables; `payops` CLI; `synthetic.py`.
- Missing contribution requires `expected > tolerance` (a 1-cent expectation is not "missing").
- Enrollment is evaluated on `pay_period_end`; terminated participants' enrollments stay open in
  the synthetic data (termination lives on the participant record).
- Files containing more than one pay period are rejected (`MIXED_PAY_PERIODS`).

## 7. Before Phase 2: install Ollama for macOS, then `ollama pull qwen3:8b`.

## 8. Phase 2 notes and deviations

- Schema v2: `agent_runs` gains `error_detail`, `tool_call_count`, status check; `tool_events`
  gains `actor` (`model` | `host`). No migrations pre-release; recreate the demo DB.
- `lookup_plan_rule` takes an optional `ruleset_version` (rules are unique per ruleset version).
- The SDK passes HOME, LOGNAME, PATH, SHELL, USER to the stdio server; we add nothing else.
- Host performs two verification reads per run (recorded as actor `host`, never shown to the
  model) so draft checks rely on ground truth rather than the model's own tool use.
- `evals/golden_cases.jsonl` holds 6 starter cases; Phase 4 extends to 30.
- Observed live (qwen3:8b, temperature 0): for exceptions with no plan rule (after-termination,
  duplicate) the model still calls `lookup_plan_rule` with invented arguments; the server returns
  `not_found` and the run continues safely. Candidate fix for Phase 4, measured by evaluation:
  expose an explicit `rule_lookup` hint (or null) in `get_exception_details` and bump prompt version.
- Live latency on M1 Max: ~25-60 s per investigation (5 model calls, first includes model load).

## 9. Phase 3 notes and deviations

- Added `operator` and `viewer` roles beside `reviewer` and the `agent` principal; reviewers may
  also start investigations so a single reviewer can run the demo end to end.
- A rejected upload stores no content, but an audit event records its SHA-256 and rejection code.
- Content-level scan (`redaction.py`) now rejects whole files with SSN/email/phone/card patterns in
  any cell (`RESTRICTED_CONTENT`), stricter than Phase 1's per-row format errors.
- Artifacts are deterministic text files (`data/artifacts/DRF-…-vN-approved.txt`), rebuilt and
  re-verified against the audited SHA-256 on every download.
- Known limits: the hash chain cannot detect truncation of the most recent events without an
  external anchor; no four-eyes rule (the editing reviewer may approve); identity is simulated.
- UI verified with Streamlit AppTest (CI smoke tests) and a headless live run: upload → Qwen3
  investigation → approve → artifact → audit chain verified (3 events).

## 10. Phase 4 notes

- Evaluation harness runs each case on a fresh synthetic DB with the real orchestrator and a stdio
  MCP server; results go to `evals/reports/*.json` and `evaluation_runs`/`evaluation_cases`.
  Partial runs (`--cases`) are flagged and can never pass the gate.
- CI exercises the harness with a scripted reference model; live evaluations run locally only.
- Measured: baseline 1.0.0 tool selection 0.571; fix 1.1.0 (`rule_lookup` hint + prompt) 0.750.
  Gate still FAILED → manifest `evaluation_status` records the failure; release remains blocked.
- Added `[agent] enabled` kill switch, `payops backup` / `verify-db` (tested restore), version
  consistency test (manifest == code), runbook/config validation tests.
- Not done (needs the maintainer): screenshots/video (screenshots added for 0.6.0), LICENSE choice (MIT for 0.6.0), real backup owner, GitHub
  remote (done for 0.6.0: CI green on the release commit), human rubric scoring of drafts.
- Fix 1.2.0 (task-scoped tool exposure + prompt): tool selection 0.893 (25/28), failed tool calls 0,
  no regressions vs 1.1.0. Gate still FAILED by one case; remaining: 2 batch-context skips,
  2 fail-closed guardrail rejections.
- Model comparison (no prompt, tool, case or threshold change): qwen3:14b PASSES (tool selection
  0.964, 27/28; case pass 29/30), fixing AGENT-015/021/023; AGENT-026 still fails safely on both
  models. 14b is about 2.2× slower (p95 118 s). Config and manifest still name qwen3:8b pending the
  maintainer's decision. Fixed: readiness now uses only evaluations of the configured model + prompt.
- Prefetch experiment (flag `[agent] prefetch`, default false; methodology `host-prefetch`, prompt
  `1.2.0+prefetch`; credit for prefetched calls only in labelled runs): qwen3:8b passes, case pass
  29/30, p95 51 s; fixes AGENT-026; AGENT-015 (8b cents/dollars error) remains, caught by guardrail.
  Chosen as release candidate 0.6.0.
- Release validation 0.6.0: two independent full runs PASSED (EVAL-20261118T005733Z, …011042Z).
  config.toml prefetch=true; package/manifest 0.6.0; manifest retrieval_mode=host-prefetch,
  prompt 1.2.0+prefetch, model digest 500a1f067a9f. Known risk: EXC-000003 cents/dollars error
  (always caught). Not committed or tagged pending maintainer approval.
- Cents fix: tools 1.2.0 add derived `display_amounts` (hashed snapshots unchanged); prompt 1.3.0
  requires quoting them. Re-validation: 2 runs PASSED (…013136Z, …014740Z); EXC-000003 8/8 correct.
  New regression AGENT-021/023 (batch summary skipped; drafts still correct). Pending decision:
  fix (another 2-run cycle) or accept in writing. Not committed.
- Prompt 1.4.0: AGENT-021/023 fixed (4/4 replays), EXC-000003 still fixed; 2 runs PASSED but
  AGENT-014/016 newly skip lookup_plan_rule (drafts correct). Prompt-only tuning of 8b moves failures
  between cases. Decision pending: prefetch the rule lookup too, accept in writing, or 14b+prefetch.
- Prompt 1.5.0: host also prefetches the applicable plan rule. Final validation: 2 runs PASSED with
  30/30 each (EVAL-20261118T032820Z, …034429Z); all earlier failures fixed, including in replays.
  Manifest evaluation_status = passed. Release 0.6.0 committed and tagged `v0.6.0`; pushed to GitHub, CI green on the
  release commit (Actions run 44325583932).

