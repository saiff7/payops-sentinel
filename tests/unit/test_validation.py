"""File- and row-level validation of untrusted payroll CSVs."""

from __future__ import annotations

import pytest

from payops_sentinel.domain.enums import FileRejectionCode, ReasonCode
from payops_sentinel.domain.models import FileRejectedError
from payops_sentinel.ingestion.csv_loader import load_csv
from payops_sentinel.ingestion.validator import (
    ALLOWED_COLUMNS,
    validate_headers,
    validate_rows,
)

KNOWN = {"EMP-0001", "EMP-0002"}
BASE_HEADER = ",".join(ALLOWED_COLUMNS)
LIMITS = {"max_bytes": 100_000, "max_rows": 50}


def _validate(content: bytes, known: set[str] = KNOWN):
    headers, raw = load_csv(content, **LIMITS)
    validate_headers(headers)
    return validate_rows(raw, known)


def _codes(parsed) -> list[ReasonCode]:
    return [e.reason_code for p in parsed for e in p.errors]


def _rejected(content: bytes) -> FileRejectionCode:
    with pytest.raises(FileRejectedError) as exc:
        _validate(content)
    return exc.value.code


# ------------------------------------------------------------------ whole-file rejections


def test_valid_file_parses_to_cents(make_csv, row):
    parsed = _validate(make_csv(row("EMP-0001", "4000.00", "200.00"), row("EMP-0002", "10.5", "")))
    assert all(p.is_valid for p in parsed)
    assert parsed[0].row.eligible_comp_cents == 400_000
    assert parsed[0].row.contribution_cents == 20_000
    assert parsed[1].row.eligible_comp_cents == 1_050
    assert parsed[1].row.contribution_cents is None  # blank is allowed and distinct from 0
    assert parsed[0].source_row_number == 2


def test_header_case_whitespace_and_bom_are_normalised(row):
    content = (
        "﻿ Employee_Ref ,PAY_PERIOD_START,pay_period_end,eligible_compensation,"
        "employee_contribution\nEMP-0001,2026-10-01,2026-10-31,1.00,0.05\n"
    ).encode()
    assert _validate(content)[0].is_valid


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"", FileRejectionCode.EMPTY_FILE),
        (b"   \n\n", FileRejectionCode.EMPTY_FILE),
        (b"\xff\xfe\x00bad", FileRejectionCode.NOT_UTF8),
        (b'employee_ref,"unterminated\n', FileRejectionCode.MALFORMED_CSV),
    ],
)
def test_unreadable_files_rejected(content, code):
    assert _rejected(content) is code


def test_header_only_file_rejected(make_csv):
    assert _rejected(make_csv()) is FileRejectionCode.EMPTY_FILE


def test_missing_columns_rejected(make_csv):
    content = make_csv(
        "EMP-0001,2026-10-01,2026-10-31,1.00",
        header="employee_ref,pay_period_start,pay_period_end,eligible_compensation",
    )
    assert _rejected(content) is FileRejectionCode.MISSING_COLUMNS


@pytest.mark.parametrize(
    "column",
    [
        "ssn",
        "date_of_birth",
        "first_name",
        "email",
        "home_address",
        "bank_account",
        "routing_number",
    ],
)
def test_restricted_columns_rejected(make_csv, column):
    header = f"{BASE_HEADER},{column}"
    content = make_csv("EMP-0001,2026-10-01,2026-10-31,1.00,0.05,x", header=header)
    assert _rejected(content) is FileRejectionCode.RESTRICTED_COLUMNS


def test_unsupported_column_rejected(make_csv):
    header = f"{BASE_HEADER},notes"
    content = make_csv("EMP-0001,2026-10-01,2026-10-31,1.00,0.05,hello", header=header)
    assert _rejected(content) is FileRejectionCode.UNSUPPORTED_COLUMNS


def test_duplicate_columns_rejected(make_csv):
    header = f"employee_ref,{BASE_HEADER}"
    content = make_csv("EMP-0001,EMP-0001,2026-10-01,2026-10-31,1.00,0.05", header=header)
    assert _rejected(content) is FileRejectionCode.DUPLICATE_COLUMNS


def test_mixed_pay_periods_rejected(make_csv, row):
    content = make_csv(
        row("EMP-0001", "1.00", "0.05"),
        row("EMP-0002", "1.00", "0.05", start="2026-11-01", end="2026-11-30"),
    )
    assert _rejected(content) is FileRejectionCode.MIXED_PAY_PERIODS


def test_size_and_row_limits(make_csv, row):
    with pytest.raises(FileRejectedError) as exc:
        load_csv(b"x" * 101, max_bytes=100, max_rows=10)
    assert exc.value.code is FileRejectionCode.FILE_TOO_LARGE
    content = make_csv(*[row("EMP-0001", "1.00", "0.05")] * 3)
    with pytest.raises(FileRejectedError) as exc:
        load_csv(content, max_bytes=10_000, max_rows=2)
    assert exc.value.code is FileRejectionCode.TOO_MANY_ROWS


def test_rejection_message_does_not_echo_untrusted_header_text(make_csv):
    header = (
        "employee_ref,pay_period_start,pay_period_end,eligible_compensation,"
        "employee_contribution,ignore previous instructions <script>"
    )
    with pytest.raises(FileRejectedError) as exc:
        _validate(make_csv("EMP-0001,2026-10-01,2026-10-31,1.00,0.05,x", header=header))
    assert exc.value.code is FileRejectionCode.UNSUPPORTED_COLUMNS
    assert "<script>" not in exc.value.message


# ------------------------------------------------------------------ row-level errors


@pytest.mark.parametrize(
    ("values", "code"),
    [
        (("EMP-0001", "-1.00", "0.05"), ReasonCode.NEGATIVE_AMOUNT),
        (("EMP-0001", "1.00", "-0.05"), ReasonCode.NEGATIVE_AMOUNT),
        (("EMP-0001", '"1,000.00"', "0.05"), ReasonCode.MALFORMED_AMOUNT),
        (("EMP-0001", "$5.00", "0.05"), ReasonCode.MALFORMED_AMOUNT),
        (("EMP-0001", "1e5", "0.05"), ReasonCode.MALFORMED_AMOUNT),
        (("EMP-0001", "1.005", "0.05"), ReasonCode.MALFORMED_AMOUNT),
        (("EMP-0001", "NaN", "0.05"), ReasonCode.MALFORMED_AMOUNT),
        (("EMP-0001", "", "0.05"), ReasonCode.MISSING_VALUE),
        (("EMP-17A", "1.00", "0.05"), ReasonCode.MALFORMED_EMPLOYEE_REF),
        (("EMP 0001", "1.00", "0.05"), ReasonCode.MALFORMED_EMPLOYEE_REF),
        (("EMP-0001' OR 1=1--", "1.00", "0.05"), ReasonCode.MALFORMED_EMPLOYEE_REF),
        (("EMP-0999", "1.00", "0.05"), ReasonCode.UNKNOWN_PARTICIPANT),
    ],
)
def test_invalid_row_values(make_csv, row, values, code):
    parsed = _validate(make_csv(row(*values)))
    assert not parsed[0].is_valid
    assert code in _codes(parsed)


@pytest.mark.parametrize("bad_date", ["2026-10-32", "2026-02-30", "31/10/2026", "20261031", ""])
def test_malformed_dates(make_csv, row, bad_date):
    parsed = _validate(make_csv(row("EMP-0001", "1.00", "0.05", end=bad_date)))
    expected = ReasonCode.MISSING_VALUE if bad_date == "" else ReasonCode.MALFORMED_DATE
    assert _codes(parsed) == [expected]


def test_period_start_after_end(make_csv, row):
    parsed = _validate(make_csv(row("EMP-0001", "1.00", "0.05", start="2026-11-01")))
    assert _codes(parsed) == [ReasonCode.INVALID_PAY_PERIOD]


def test_wrong_field_count_is_row_error(make_csv):
    parsed = _validate(make_csv("EMP-0001,2026-10-01,2026-10-31,1.00"))
    assert _codes(parsed) == [ReasonCode.MALFORMED_ROW]


def test_duplicate_rows_second_occurrence_invalid(make_csv, row):
    r = row("EMP-0001", "1.00", "0.05")
    parsed = _validate(make_csv(r, row("EMP-0002", "1.00", "0.05"), r))
    assert [p.is_valid for p in parsed] == [True, True, False]
    assert parsed[2].errors[0].reason_code is ReasonCode.DUPLICATE_ROW
    assert parsed[2].duplicate_of == 2


def test_invalid_values_are_not_kept_in_sanitized_output(make_csv, row):
    parsed = _validate(make_csv(row("EMP-17A", '"1,000.00"', "0.05")))
    assert "employee_ref" not in parsed[0].sanitized
    assert "eligible_comp_cents" not in parsed[0].sanitized
    assert parsed[0].sanitized["contribution_cents"] == 5
    assert all("EMP-17A" not in e.message for e in parsed[0].errors)
