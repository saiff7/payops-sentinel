"""Deterministic synthetic dataset generator for the fictional Northstar Manufacturing.

All people are aliases (EMP-NNNN); no real personal data exists anywhere in the dataset.
Also writes ``evals/expected_reconciliation.json``, an oracle derived from the *injected*
anomalies and an independent integer implementation of the contribution formula.
"""

from __future__ import annotations

import csv
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SEED = 20261031
PERIOD_START, PERIOD_END = "2026-10-01", "2026-10-31"
PLAN_ID = "PLAN-401K-A"
RULESET_VERSION = "2026.10-demo"
RATES_BP = (300, 400, 500, 600, 800, 1000)
PARTICIPANT_COUNT = 40
HEADER = [
    "employee_ref",
    "pay_period_start",
    "pay_period_end",
    "eligible_compensation",
    "employee_contribution",
]

TERMINATIONS = {"EMP-0031": "2026-09-15", "EMP-0032": "2026-10-20"}
RULE_DESCRIPTION = (
    "Employee deferral = eligible compensation x elected rate, rounded half-up to the cent; "
    "differences up to 1 cent are tolerated. Demonstration rule only, not a regulatory calculation."
)
RULE_SOURCE = "Synthetic Northstar plan configuration, section 3.1 (demo)"
RATE_OVERRIDES = {"EMP-0008": 600, "EMP-0017": 500, "EMP-0036": 0}


@dataclass(frozen=True)
class _Enr:
    enrollment_id: str
    employee_ref: str
    elected_rate_bp: int
    effective_from: str
    effective_to: str | None = None
    status: str = "active"


def _ref(n: int) -> str:
    return f"EMP-{n:04d}"


def _oracle_cents(comp_cents: int, bp: int) -> int:
    """Independent half-up implementation (integer arithmetic) used only as a test oracle."""
    return (comp_cents * bp + 5_000) // 10_000


def _money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def _enrollments() -> list[_Enr]:
    out = []
    for n in range(1, PARTICIPANT_COUNT + 1):
        ref = _ref(n)
        rate = RATE_OVERRIDES.get(ref, RATES_BP[(n - 1) % len(RATES_BP)])
        enr = _Enr(f"ENR-{n:04d}", ref, rate, "2025-01-01")
        if ref == "EMP-0033":
            enr = _Enr(enr.enrollment_id, ref, 500, "2025-01-01", status="inactive")  # opted out
        elif ref == "EMP-0034":
            enr = _Enr(enr.enrollment_id, ref, 400, "2026-11-01")  # enrolls next period
        elif ref == "EMP-0035":
            enr = _Enr(enr.enrollment_id, ref, 600, PERIOD_END)  # boundary: effective on period end
        elif ref == "EMP-0037":
            enr = _Enr(enr.enrollment_id, ref, 300, "2025-01-01", "2026-10-30")  # superseded
            out.append(_Enr("ENR-0041", ref, 700, PERIOD_END))
        out.append(enr)
    return sorted(out, key=lambda e: e.enrollment_id)


def _active_rate(enrs: list[_Enr], ref: str) -> int:
    for e in enrs:
        if (
            e.employee_ref == ref
            and e.status == "active"
            and e.effective_from <= PERIOD_END
            and (e.effective_to is None or e.effective_to >= PERIOD_END)
        ):
            return e.elected_rate_bp
    return 0


def build_dataset() -> dict[str, Any]:
    rng = random.Random(SEED)  # noqa: S311 - deterministic synthetic data, not security
    enrs = _enrollments()
    comp = {_ref(n): rng.randrange(250_000, 900_000) for n in range(1, PARTICIPANT_COUNT + 1)}
    comp["EMP-0017"] = 400_000  # blueprint example: $4,000.00 at 5% -> $200.00

    expected = {ref: _oracle_cents(c, _active_rate(enrs, ref)) for ref, c in comp.items()}
    refs = [r for r in comp if r != "EMP-0031"]  # terminated before the period: not paid
    clean = [[r, PERIOD_START, PERIOD_END, _money(comp[r]), _money(expected[r])] for r in refs]

    rows = [list(r) for r in clean]
    idx = {r[0]: i for i, r in enumerate(rows)}
    oracle: list[dict[str, Any]] = []

    def row_no(i: int) -> int:
        return i + 2

    def flag(ref: str, etype: str, reason: str, exp: int | None, act: int | None, i: int) -> None:
        stored_ref = ref if re.fullmatch(r"EMP-\d{4}", ref) else None  # malformed refs never stored
        item = {
            "source_row_number": row_no(i),
            "employee_ref": stored_ref,
            "exception_type": etype,
            "reason_code": reason,
        }
        if etype != "DUPLICATE_OR_INVALID_RECORD":
            item |= {"expected_cents": exp, "actual_cents": act}
        oracle.append(item)

    # Missing contributions
    rows[idx["EMP-0017"]][4] = "0.00"
    flag(
        "EMP-0017", "MISSING_CONTRIBUTION", "CONTRIBUTION_ZERO_OR_BLANK", 20_000, 0, idx["EMP-0017"]
    )
    rows[idx["EMP-0005"]][4] = ""
    flag(
        "EMP-0005",
        "MISSING_CONTRIBUTION",
        "CONTRIBUTION_ZERO_OR_BLANK",
        expected["EMP-0005"],
        0,
        idx["EMP-0005"],
    )
    # Incorrect rate: 3% applied instead of 6%; 2 cents over tolerance; contribution w/o enrollment
    wrong = _oracle_cents(comp["EMP-0008"], 300)
    rows[idx["EMP-0008"]][4] = _money(wrong)
    flag(
        "EMP-0008",
        "INCORRECT_CONTRIBUTION_RATE",
        "CONTRIBUTION_RATE_MISMATCH",
        expected["EMP-0008"],
        wrong,
        idx["EMP-0008"],
    )
    rows[idx["EMP-0012"]][4] = _money(expected["EMP-0012"] + 2)
    flag(
        "EMP-0012",
        "INCORRECT_CONTRIBUTION_RATE",
        "CONTRIBUTION_RATE_MISMATCH",
        expected["EMP-0012"],
        expected["EMP-0012"] + 2,
        idx["EMP-0012"],
    )
    rows[idx["EMP-0013"]][4] = _money(expected["EMP-0013"] - 1)  # within 1-cent tolerance
    rows[idx["EMP-0034"]][4] = "100.00"
    flag(
        "EMP-0034",
        "INCORRECT_CONTRIBUTION_RATE",
        "NO_ACTIVE_ENROLLMENT",
        0,
        10_000,
        idx["EMP-0034"],
    )
    # Invalid values in existing rows (no raw value is ever stored)
    rows[idx["EMP-0021"]][3] = "-1500.00"
    rows[idx["EMP-0023"]][2] = "2026-10-32"
    rows[idx["EMP-0024"]][4] = "12,00"
    for ref, reason in (
        ("EMP-0021", "NEGATIVE_AMOUNT"),
        ("EMP-0023", "MALFORMED_DATE"),
        ("EMP-0024", "MALFORMED_AMOUNT"),
    ):
        flag(ref, "DUPLICATE_OR_INVALID_RECORD", reason, None, None, idx[ref])
    # Appended rows
    appended = [
        (
            ["EMP-0031", PERIOD_START, PERIOD_END, "3200.00", "160.00"],
            ("CONTRIBUTION_AFTER_TERMINATION", "PERIOD_STARTS_AFTER_TERMINATION", None, 16_000),
        ),
        (
            list(clean[idx["EMP-0020"]]),
            ("DUPLICATE_OR_INVALID_RECORD", "DUPLICATE_ROW", None, None),
        ),
        (
            ["EMP-17A", PERIOD_START, PERIOD_END, "4100.00", "205.00"],
            ("DUPLICATE_OR_INVALID_RECORD", "MALFORMED_EMPLOYEE_REF", None, None),
        ),
        (
            ["EMP-0999", PERIOD_START, PERIOD_END, "5000.00", "250.00"],
            ("DUPLICATE_OR_INVALID_RECORD", "UNKNOWN_PARTICIPANT", None, None),
        ),
    ]
    for row, (etype, reason, exp, act) in appended:
        rows.append(row)
        flag(row[0], etype, reason, exp, act, len(rows) - 1)

    oracle.sort(key=lambda o: o["source_row_number"])
    invalid = sum(o["exception_type"] == "DUPLICATE_OR_INVALID_RECORD" for o in oracle)
    return {
        "enrollments": enrs,
        "clean": clean,
        "with_exceptions": rows,
        "expected": {
            "ruleset_version": RULESET_VERSION,
            "payroll_clean.csv": {
                "total_rows": len(clean),
                "valid_rows": len(clean),
                "invalid_rows": 0,
                "exceptions": [],
            },
            "payroll_with_exceptions.csv": {
                "total_rows": len(rows),
                "valid_rows": len(rows) - invalid,
                "invalid_rows": invalid,
                "exceptions": oracle,
            },
        },
    }


def _write_csv(path: Path, header: list[str], rows: list[list[Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def write_dataset(data_dir: Path, expected_path: Path) -> None:
    data = build_dataset()
    data_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        data_dir / "employer.json",
        {
            "employer_id": "ER-NORTHSTAR",
            "employer_code": "NORTHSTAR",
            "name": "Northstar Manufacturing (synthetic)",
        },
    )
    _write_csv(
        data_dir / "participants.csv",
        ["participant_id", "employee_ref", "termination_date"],
        [
            [f"PRT-{n:04d}", _ref(n), TERMINATIONS.get(_ref(n), "")]
            for n in range(1, PARTICIPANT_COUNT + 1)
        ],
    )
    _write_csv(
        data_dir / "enrollments.csv",
        [
            "enrollment_id",
            "employee_ref",
            "plan_id",
            "elected_rate_bp",
            "effective_from",
            "effective_to",
            "status",
        ],
        [
            [
                e.enrollment_id,
                e.employee_ref,
                PLAN_ID,
                e.elected_rate_bp,
                e.effective_from,
                e.effective_to or "",
                e.status,
            ]
            for e in data["enrollments"]
        ],
    )
    _write_json(
        data_dir / "plan_rules.json",
        {
            "ruleset_version": RULESET_VERSION,
            "data_classification": "SYNTHETIC_DEMO",
            "rules": [
                {
                    "rule_id": "RULE-DEFERRAL-PCT-V1",
                    "plan_id": PLAN_ID,
                    "rule_code": "EMPLOYEE_DEFERRAL_PCT",
                    "version": "1",
                    "effective_from": "2026-01-01",
                    "effective_to": None,
                    "rounding": "ROUND_HALF_UP",
                    "tolerance_cents": 1,
                    "description": RULE_DESCRIPTION,
                    "source_reference": RULE_SOURCE,
                }
            ],
        },
    )
    _write_csv(data_dir / "payroll_clean.csv", HEADER, data["clean"])
    _write_csv(data_dir / "payroll_with_exceptions.csv", HEADER, data["with_exceptions"])
    expected_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(expected_path, data["expected"])
