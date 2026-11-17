"""File- and row-level validation. Untrusted text is only ever matched, never echoed or stored."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from typing import Any

from payops_sentinel.domain.enums import FileRejectionCode, ReasonCode
from payops_sentinel.domain.models import (
    EMPLOYEE_REF_PATTERN,
    FileRejectedError,
    ParsedRow,
    PayrollRow,
    RowError,
)
from payops_sentinel.ingestion.csv_loader import RawRow
from payops_sentinel.redaction import find_restricted

ALLOWED_COLUMNS: tuple[str, ...] = (
    "employee_ref",
    "pay_period_start",
    "pay_period_end",
    "eligible_compensation",
    "employee_contribution",
)
# Header fragments that indicate restricted personal or financial data.
RESTRICTED_HEADER_TOKENS: tuple[str, ...] = (
    "ssn",
    "social",
    "tax_id",
    "birth",
    "dob",
    "name",
    "email",
    "mail",
    "address",
    "street",
    "city",
    "zip",
    "postal",
    "phone",
    "mobile",
    "bank",
    "account",
    "routing",
    "iban",
    "swift",
    "passport",
    "license",
    "salary_history",
    "gender",
    "ethnic",
)

_REF_RE = re.compile(EMPLOYEE_REF_PATTERN)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONEY_RE = re.compile(r"^\d{1,9}(\.\d{1,2})?$")
_NEG_MONEY_RE = re.compile(r"^-\d{1,9}(\.\d{1,2})?$")
_SAFE_HEADER_RE = re.compile(r"[^a-z0-9_ ]")


def _safe_header(name: str) -> str:
    return _SAFE_HEADER_RE.sub("?", name)[:40] or "<blank>"


def validate_headers(headers: list[str]) -> None:
    if len(set(headers)) != len(headers):
        raise FileRejectedError(FileRejectionCode.DUPLICATE_COLUMNS, "duplicate column headers")
    extra = [h for h in headers if h not in ALLOWED_COLUMNS]
    restricted = [h for h in extra if any(tok in h for tok in RESTRICTED_HEADER_TOKENS)]
    if restricted:
        names = ", ".join(sorted(_safe_header(h) for h in restricted))
        raise FileRejectedError(
            FileRejectionCode.RESTRICTED_COLUMNS, f"restricted columns not accepted: {names}"
        )
    if extra:
        names = ", ".join(sorted(_safe_header(h) for h in extra))
        raise FileRejectedError(
            FileRejectionCode.UNSUPPORTED_COLUMNS, f"unsupported columns: {names}"
        )
    missing = [c for c in ALLOWED_COLUMNS if c not in headers]
    if missing:
        raise FileRejectedError(
            FileRejectionCode.MISSING_COLUMNS, f"missing required columns: {', '.join(missing)}"
        )


def _parse_money(
    raw: str, field: str, row: int, *, required: bool
) -> tuple[int | None, RowError | None]:
    if raw == "":
        if required:
            return None, RowError(
                source_row_number=row,
                field=field,
                reason_code=ReasonCode.MISSING_VALUE,
                message="value is required",
            )
        return None, None
    if _NEG_MONEY_RE.match(raw):
        return None, RowError(
            source_row_number=row,
            field=field,
            reason_code=ReasonCode.NEGATIVE_AMOUNT,
            message="amount must not be negative",
        )
    if not _MONEY_RE.match(raw):
        return None, RowError(
            source_row_number=row,
            field=field,
            reason_code=ReasonCode.MALFORMED_AMOUNT,
            message="amount must be a plain decimal with at most 2 places",
        )
    return int(Decimal(raw) * 100), None


def _parse_date(raw: str, field: str, row: int) -> tuple[date | None, RowError | None]:
    if raw == "":
        return None, RowError(
            source_row_number=row,
            field=field,
            reason_code=ReasonCode.MISSING_VALUE,
            message="value is required",
        )
    try:
        if not _DATE_RE.match(raw):
            raise ValueError
        return date.fromisoformat(raw), None
    except ValueError:
        return None, RowError(
            source_row_number=row,
            field=field,
            reason_code=ReasonCode.MALFORMED_DATE,
            message="date must be a valid YYYY-MM-DD value",
        )


def _parse_row(raw: RawRow, known_refs: set[str]) -> ParsedRow:
    n = raw.source_row_number
    if raw.values is None:
        err = RowError(
            source_row_number=n,
            field="*",
            reason_code=ReasonCode.MALFORMED_ROW,
            message="field count does not match header",
        )
        return ParsedRow(source_row_number=n, row=None, sanitized={}, errors=(err,))

    v = raw.values
    errors: list[RowError] = []
    clean: dict[str, Any] = {}

    ref = v["employee_ref"]
    if not _REF_RE.match(ref):
        errors.append(
            RowError(
                source_row_number=n,
                field="employee_ref",
                reason_code=ReasonCode.MALFORMED_EMPLOYEE_REF,
                message="employee_ref must match EMP-NNNN",
            )
        )
    else:
        clean["employee_ref"] = ref
        if ref not in known_refs:
            errors.append(
                RowError(
                    source_row_number=n,
                    field="employee_ref",
                    reason_code=ReasonCode.UNKNOWN_PARTICIPANT,
                    message="employee_ref is not in the participant register",
                )
            )

    for field in ("pay_period_start", "pay_period_end"):
        value, err = _parse_date(v[field], field, n)
        if err:
            errors.append(err)
        else:
            clean[field] = value
    start, end = clean.get("pay_period_start"), clean.get("pay_period_end")
    if start and end and start > end:
        errors.append(
            RowError(
                source_row_number=n,
                field="pay_period_start",
                reason_code=ReasonCode.INVALID_PAY_PERIOD,
                message="pay_period_start is after pay_period_end",
            )
        )

    comp, err = _parse_money(v["eligible_compensation"], "eligible_compensation", n, required=True)
    if err:
        errors.append(err)
    else:
        clean["eligible_comp_cents"] = comp
    contrib, err = _parse_money(
        v["employee_contribution"], "employee_contribution", n, required=False
    )
    if err:
        errors.append(err)
    else:
        clean["contribution_cents"] = contrib

    row = None if errors else PayrollRow(source_row_number=n, **clean)
    return ParsedRow(source_row_number=n, row=row, sanitized=clean, errors=tuple(errors))


def _mark_duplicates(parsed: list[ParsedRow]) -> list[ParsedRow]:
    """Second and later valid rows for the same (employee_ref, pay_period_end) are invalid."""
    first_seen: dict[tuple[str, date], int] = {}
    out: list[ParsedRow] = []
    for p in parsed:
        if p.row is None:
            out.append(p)
            continue
        key = (p.row.employee_ref, p.row.pay_period_end)
        if key not in first_seen:
            first_seen[key] = p.source_row_number
            out.append(p)
            continue
        err = RowError(
            source_row_number=p.source_row_number,
            field="employee_ref",
            reason_code=ReasonCode.DUPLICATE_ROW,
            message=f"duplicate of row {first_seen[key]} for the same pay period",
        )
        out.append(
            p.model_copy(update={"row": None, "errors": (err,), "duplicate_of": first_seen[key]})
        )
    return out


def batch_pay_period(parsed: Iterable[ParsedRow]) -> tuple[date, date] | None:
    """The single pay period of the file; files mixing periods are rejected."""
    periods = {
        (p.sanitized["pay_period_start"], p.sanitized["pay_period_end"])
        for p in parsed
        if "pay_period_start" in p.sanitized and "pay_period_end" in p.sanitized
    }
    if len(periods) > 1:
        raise FileRejectedError(
            FileRejectionCode.MIXED_PAY_PERIODS, "a file must contain exactly one pay period"
        )
    return next(iter(periods), None)


def reject_restricted_content(raw_rows: list[RawRow]) -> None:
    """Header checks cannot catch personal data typed into allowed columns; scan every cell."""
    for raw in raw_rows:
        for value in (raw.values or {}).values():
            found = find_restricted(value)
            if found:
                raise FileRejectedError(
                    FileRejectionCode.RESTRICTED_CONTENT,
                    f"row {raw.source_row_number} contains restricted data ({', '.join(found)})",
                )


def validate_rows(raw_rows: list[RawRow], known_refs: set[str]) -> list[ParsedRow]:
    reject_restricted_content(raw_rows)
    parsed = _mark_duplicates([_parse_row(r, known_refs) for r in raw_rows])
    batch_pay_period(parsed)
    return parsed
