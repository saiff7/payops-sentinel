# Operational runbook

Audience: whoever operates the local demo. Commands run from the repository root.
The deterministic exception queue never depends on the AI path. When anything AI-related fails,
investigations fall back to **manual review** and nothing is auto-approved.

## Start, stop, health

```bash
brew services start ollama          # local model runtime (or open the Ollama app)
ollama list                         # must show qwen3:8b
uv sync                             # dependencies from uv.lock
uv run streamlit run src/payops_sentinel/ui/app.py
uv run payops verify-db             # integrity + schema version + audit chain
```

Health signals are on the **Governance** page: agent runs by status and error category, MCP tool
failures, review queue age, and audit-chain verification.

## Failure procedures

| Failure | Symptom | Action |
|---|---|---|
| Ollama unavailable | Runs end `manual_review` / `MODEL_UNAVAILABLE` | `brew services restart ollama`; `curl 127.0.0.1:11434/api/version`. Meanwhile investigate manually from the evidence panel. |
| Model missing | `MODEL_UNAVAILABLE: model qwen3:8b is not installed locally` | `ollama pull qwen3:8b`; check that the digest in `agent_runs.model_version` matches the evaluated release. |
| MCP server crashes | `MCP_UNAVAILABLE`, `TRANSPORT_ERROR` tool event | Run `uv run python -m payops_sentinel.mcp_server.server --db data/payops.db --manifest governance/agent_manifest.toml` and read stderr; fix, then rerun the investigation. |
| MCP contract mismatch | `MCP_CONTRACT_MISMATCH` | A tool is missing or not read-only. Do not change the allowlist to "fix" it; restore the released server code. |
| Database locked | `database is locked` | Close other writers (CLI, second UI). The busy timeout is 5 s; retrying is safe because writes are transactional. |
| Database corrupted | `verify-db` reports UNHEALTHY or integrity fails | Stop the UI and restore the last verified backup (see below). |
| Invalid payroll file | Upload shows `File rejected — CODE` | Nothing is stored; a `BATCH_REJECTED` audit event records the hash and code. Correct the file and resubmit. |
| Agent evaluation fails | `payops evaluate` exits 5 / gate FAILED | Block the release; compare with `payops eval-compare old.json new.json` and fix the regression. |
| New version misbehaves | Unexpected drafts or rising manual-review rate | Disable the agent (below), then roll back (below). |
| Reviewer service fails | Errors on approve/reject | Drafts are preserved; stop approvals until fixed. No partial approvals: each action is one transaction. |
| Audit chain broken | `verify-db` / Governance shows "Chain broken at sequence N" | Treat as a security incident: preserve the file, restore the last verified backup, investigate access. |

## Retrieval mode

Release 0.6.0 ships with `[agent] prefetch = true` (`host-prefetch`). Setting it to `false` returns
to fully model-selected tool use, which is a different evaluated configuration (prompt version
without `+prefetch`; release gate FAILED for qwen3:8b). Never change it in operation without a full `payops evaluate` of
the resulting configuration; the Governance readiness check only counts evaluations of the
configured model and prompt version.

## Disable AI investigation (kill switch)

Set `enabled = false` under `[agent]` in `config.toml` and restart the UI. Every investigation
then records `AGENT_DISABLED` and goes to manual review. Ingestion, reconciliation, evidence and
review keep working.

## Roll back an agent release

1. Disable the agent (above).
2. `git checkout <last-release-tag>` (each release records the commit, model digest, prompt and
   tools version, and the evaluation report in `evals/reports/`).
3. `ollama pull` the model recorded for that release, and confirm the digest with `ollama list`.
4. `uv sync --locked`, then `uv run payops evaluate`: the gate must pass again.
5. Re-enable the agent.

## Backup and restore (tested)

```bash
uv run payops backup backups/payops-$(date +%Y%m%d-%H%M).db   # online backup API + verification
uv run payops verify-db backups/<file>.db                      # must print HEALTHY
```

Restore: stop the UI, move the damaged `data/payops.db` aside (keep it for investigation), copy the
verified backup to `data/payops.db`, run `uv run payops verify-db`, then start the UI. Never copy a
live database file directly; always use `payops backup`. The restore path is exercised by
`tests/unit/test_backup.py`.

## Routine checks

- Daily (when in use): `payops verify-db`; review queue age on the Governance page.
- Each release: see `docs/release_checklist.md`.
- Pre-release schema changes are not migrated: delete the demo DB and run `payops init`.
