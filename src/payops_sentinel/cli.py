"""Command-line entry point for operating the deterministic engine without the UI."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from payops_sentinel.config import PROJECT_ROOT, Settings, load_settings
from payops_sentinel.domain.models import ConfigurationError, FileRejectedError
from payops_sentinel.ingestion.reference_loader import seed_reference_data
from payops_sentinel.ingestion.service import ingest_payroll_file
from payops_sentinel.storage import repositories as repo
from payops_sentinel.storage.db import connect, initialize
from payops_sentinel.synthetic import write_dataset


def _open(settings: Settings):
    conn = connect(settings.db_path)
    initialize(conn)
    return conn


def _money(cents: int | None) -> str:
    return "-" if cents is None else f"${cents / 100:,.2f}"


def cmd_generate(settings: Settings, _: argparse.Namespace) -> int:
    write_dataset(
        settings.reference_data_dir, PROJECT_ROOT / "evals" / "expected_reconciliation.json"
    )
    print(f"synthetic data written to {settings.reference_data_dir}")
    return 0


def cmd_init(settings: Settings, _: argparse.Namespace) -> int:
    conn = _open(settings)
    employer = seed_reference_data(conn, settings.reference_data_dir)
    print(f"database ready at {settings.db_path} (employer {employer.employer_code})")
    return 0


def cmd_ingest(settings: Settings, args: argparse.Namespace) -> int:
    path = Path(args.csv)
    conn = _open(settings)
    try:
        result = ingest_payroll_file(conn, settings, content=path.read_bytes(), file_name=path.name)
    except FileRejectedError as exc:
        print(f"REJECTED [{exc.code}] {exc.message}", file=sys.stderr)
        return 2
    except ConfigurationError as exc:
        print(f"CONFIGURATION ERROR: {exc} (run `payops init` first?)", file=sys.stderr)
        return 3
    state = "created" if result.created else "already processed (idempotent)"
    print(
        f"{result.batch_id}: {state}; rows={result.total_rows} valid={result.valid_rows} "
        f"invalid={result.invalid_rows} exceptions={result.exception_count}"
    )
    return 0


def cmd_show(settings: Settings, args: argparse.Namespace) -> int:
    conn = _open(settings)
    batch = repo.get_batch(conn, args.batch_id)
    if batch is None:
        print(f"batch {args.batch_id} not found", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                k: batch[k]
                for k in (
                    "batch_id",
                    "file_name",
                    "pay_period_end",
                    "status",
                    "total_rows",
                    "invalid_rows",
                    "exception_count",
                )
            }
        )
    )
    for e in repo.list_exceptions(conn, args.batch_id):
        print(
            f"{e['exception_id']} row {e['source_row_number']:>3} {e['employee_ref'] or '-':9} "
            f"{e['exception_type']:31} {e['reason_code']:33} expected={_money(e['expected_cents'])}"
            f" actual={_money(e['actual_cents'])}"
        )
        if args.evidence:
            for ev in repo.list_evidence(conn, e["exception_id"]):
                print(f"    {ev['evidence_id']} {ev['evidence_type']:17} {ev['source_reference']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="payops", description=__doc__)
    parser.add_argument("--config", type=Path, default=None, help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("generate-data", help="regenerate deterministic synthetic data")
    sub.add_parser("init", help="create the database and load reference data")
    p_ingest = sub.add_parser("ingest", help="process a payroll CSV")
    p_ingest.add_argument("csv")
    p_show = sub.add_parser("show", help="show a batch and its exceptions")
    p_show.add_argument("batch_id")
    p_show.add_argument("--evidence", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = load_settings(args.config)
    handlers = {
        "generate-data": cmd_generate,
        "init": cmd_init,
        "ingest": cmd_ingest,
        "show": cmd_show,
    }
    return handlers[args.command](settings, args)


if __name__ == "__main__":
    raise SystemExit(main())
