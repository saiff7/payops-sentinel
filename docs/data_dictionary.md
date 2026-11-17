# Data dictionary

All data is **SYNTHETIC_DEMO**. Money is stored as integer **cents**; rates as **basis points**
(500 = 5%); dates as ISO-8601 text. Schema source: `src/payops_sentinel/storage/schema.sql` (v2).
Field classifications are listed in `governance/data_classification.toml`.

## Upload contract (CSV)

| Column | Format | Notes |
|---|---|---|
| `employee_ref` | `EMP-NNNN` | Synthetic alias only; must exist in the participant register |
| `pay_period_start` / `pay_period_end` | `YYYY-MM-DD` | One pay period per file; start ≤ end |
| `eligible_compensation` | plain decimal, ≤ 2 places, ≥ 0 | e.g. `4000.00` |
| `employee_contribution` | plain decimal, ≤ 2 places, ≥ 0, or blank | blank ≠ 0 but both count as missing |

Any other column is rejected. Restricted headers (SSN, DOB, name, email, address, bank, routing…)
or restricted cell content (SSN, email, phone, card or account patterns) reject the whole file.

## Tables

| Table | Key columns | Purpose |
|---|---|---|
| `employers` | `employer_id`, `employer_code` | Synthetic employer (Northstar Manufacturing) |
| `participants` | `participant_id`, `employee_ref`, `termination_date` | Participant register |
| `plan_enrollments` | `elected_rate_bp`, `effective_from/to`, `status` | Effective-dated elections |
| `plan_rules` | `rule_id`, `ruleset_version`, `rule_json` (rounding, tolerance) | Versioned rules |
| `payroll_batches` | `batch_id`, `file_sha256`, counts, `status` | Unique (employer, sha256, ruleset) |
| `payroll_rows` | `source_row_number`, sanitised values, `validation_status` | Only format-valid values |
| `validation_errors` | `field`, `reason_code`, `message` | Never echoes raw values |
| `exceptions` | `exception_type`, `reason_code`, `expected_cents`, `actual_cents`, `status` | Findings |
| `evidence` | `evidence_type`, `source_reference`, `snapshot_json`, `content_hash` | Hashed facts |
| `agent_runs` | `model_version` (incl. digest), `prompt_version`, `status`, `error_category` | Runs |
| `tool_events` | `actor` (model/host), `tool_name`, `arguments_json`, `result_hash`, `success` | Calls |
| `drafts` | (`draft_id`, `draft_version`), `author_id`, `draft_json`, `content_hash`, `status` | Versions |
| `approvals` | `reviewer_id`, `action` (approve/reject), `content_hash` | Append-only |
| `audit_events` | `sequence`, `previous_hash`, `event_hash` | Append-only hash chain |
| `agent_assets` | registry fields | Reserved; the manifest TOML is the source of truth |
| `evaluation_runs` / `evaluation_cases` | metrics, per-case results | Evaluation history |

## Enumerations

- **Exception types:** `MISSING_CONTRIBUTION`, `INCORRECT_CONTRIBUTION_RATE`,
  `CONTRIBUTION_AFTER_TERMINATION`, `DUPLICATE_OR_INVALID_RECORD`.
- **Exception status:** `open` → `under_investigation` (draft pending) / `manual_review` → `closed`.
- **Draft status:** `pending_review` → `approved` | `rejected` | `superseded` (by a newer version).
- **Agent failure categories:** `EXCEPTION_NOT_FOUND`, `MCP_UNAVAILABLE`, `MCP_CONTRACT_MISMATCH`,
  `MODEL_UNAVAILABLE`, `REPEATED_FAILED_CALL`, `INVALID_OUTPUT`, `EVIDENCE_MISMATCH`,
  `UNSUPPORTED_CONTENT`, `INTERNAL_ERROR`, `AGENT_DISABLED`.
- **Audit events:** `BATCH_INGESTED`, `BATCH_REJECTED`, `INVESTIGATION_COMPLETED`, `DRAFT_EDITED`,
  `DRAFT_APPROVED`, `DRAFT_REJECTED`, `ARTIFACT_DOWNLOADED`, `AUTHORIZATION_DENIED`.

## Calculation (demonstration rule, not a regulatory calculation)

`expected_cents = ROUND_HALF_UP(eligible_comp_cents × elected_rate_bp / 10000)`; a difference of up
to the rule's `tolerance_cents` (1) is accepted. Enrollment is evaluated on `pay_period_end`.
