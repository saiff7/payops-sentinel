# Threat model

Scope: the local demonstration (single machine, synthetic data, simulated identities).
Method: assets → trust boundaries → threats → implemented controls → residual risk.
Each control below is backed by an executable test (file shown).

## Assets

Payroll-linked records (CONFIDENTIAL, synthetic), reviewer decisions and the audit trail,
approved communications, the agent configuration (model, prompt, tool allowlist).

## Trust boundaries

1. Uploaded CSV → application (untrusted input).
2. Application ⇄ local model (the model's output is untrusted).
3. Agent host ⇄ MCP server subprocess (stdio: same user, **not a sandbox**).
4. UI user → review service (identity simulated locally).
5. Application → SQLite file (anyone with file access can modify it).

## Threats and controls

| # | Threat | Control | Test |
|---|---|---|---|
| T1 | Real personal data uploaded | Column allowlist; restricted headers and cell patterns reject the whole file; raw values never stored | `security/test_redaction.py`, `unit/test_validation.py` |
| T2 | Prompt injection via uploaded data | Fields strictly typed (alias regex, ISO dates, decimals), so no free text reaches the model; tool results labelled as data | `security/test_redaction.py::test_raw_upload_values_never_reach_the_model` |
| T3 | Prompt injection via the objective ("approve", "send", "delete") | No such tools exist; the allowlist rejects unknown names; adversarial evaluation cases | `agent/test_orchestrator.py`, `evals/` (adversarial cases) |
| T4 | Model invents tools or arguments | Host allowlist + Pydantic argument models (`extra="forbid"`); repeated failures stop the run | `agent/test_orchestrator.py` |
| T5 | Hallucinated facts in drafts | Evidence IDs must belong to the exception and have been retrieved; `$` amounts and `EMP-` refs must appear in the evidence; human review is mandatory | `agent/test_orchestrator.py` |
| T6 | Agent approves or sends | Agent role holds only `draft.create`; approval needs the reviewer role plus an exact version and hash; no send capability exists | `security/test_permissions.py` |
| T7 | Reviewer approves a draft other than the one they read | Version and content-hash check; superseded or altered drafts are refused | `review/test_approval_audit.py` |
| T8 | Tampering with decisions | Append-only triggers on approvals and audit; draft content immutable; hash chain detects altered or removed events | `review/test_approval_audit.py` |
| T9 | MCP server writes or leaks | Only read tools; `mode=ro` + `query_only`; DB hash unchanged after calls; crashes masked by the SDK | `mcp_server/test_tools.py` |
| T10 | Compromised or unexpected MCP server | Host requires exactly the allowlisted tools and read-only annotations; extra tools are never offered | `agent/test_orchestrator.py` |
| T11 | Data leaves the machine | Ollama host must be localhost; `:cloud` models rejected; no external APIs | `agent/test_orchestrator.py::test_configuration_rejects…` |
| T12 | Resource exhaustion by the agent | Turn, call, result-size and timeout limits | `agent/test_orchestrator.py` |
| T13 | Secrets leak into the MCP subprocess | The SDK passes only HOME, LOGNAME, PATH, SHELL and USER; we add nothing; no credentials exist | review of `server_parameters()` |
| T14 | Sensitive values in logs or telemetry | Tool events hold identifiers only; no prompts or amounts are logged | `security/test_redaction.py` |

## Residual risks (accepted for the demo; production actions)

- **Identity is simulated.** Production: OIDC (e.g. Entra ID via `st.login`) for authentication,
  and central RBAC for authorisation. Authentication alone must not grant approval rights.
- **SQLite file access defeats controls.** The audit chain is tamper-evident, not immutable, and
  deleting the newest events cannot be detected without an external anchor. Production: managed
  database with service identities, and audit export to independently controlled append-only storage.
- **stdio is not isolation.** The MCP server runs as the same OS user. Production: a separate
  workload or service identity with read-only database credentials.
- **No four-eyes rule.** A reviewer may approve their own edit. Production: require a second
  reviewer for edited drafts.
- **Regex-based PII detection** has false negatives (for example, unusual formats). Synthetic-only
  data is the primary control.
