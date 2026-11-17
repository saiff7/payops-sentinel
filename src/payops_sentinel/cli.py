"""Command-line entry point for operating the deterministic engine without the UI."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
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


def cmd_investigate(settings: Settings, args: argparse.Namespace) -> int:
    from payops_sentinel.agent.orchestrator import run_investigation  # AI deps load lazily

    outcome = run_investigation(settings, args.exception_id)
    print(
        f"{outcome.run_id} {outcome.exception_id}: {outcome.status.value} "
        f"model={outcome.model_version} duration_ms={outcome.duration_ms}"
    )
    if outcome.failure_category:
        print(f"  manual review: {outcome.failure_category.value} ({outcome.failure_detail})")
    for c in outcome.tool_calls:
        state = "ok" if c.success else f"FAILED {c.error_category}"
        print(
            f"  #{c.sequence} [{c.actor}] {c.tool_name}({json.dumps(c.arguments)}) {state} "
            f"{c.duration_ms}ms"
        )
    if outcome.draft:
        print(f"draft {outcome.draft_id} v{outcome.draft_version} sha256={outcome.content_hash}")
        print(json.dumps(outcome.draft.model_dump(mode="json"), indent=2))
    return 0 if outcome.draft else 4


def cmd_evaluate(settings: Settings, args: argparse.Namespace) -> int:
    from payops_sentinel.operations import evaluations as ev

    dataset = PROJECT_ROOT / "evals" / "golden_cases.jsonl"
    cases = ev.load_cases(dataset)
    if args.cases:
        wanted = set(args.cases.split(","))
        cases = [c for c in cases if c.case_id in wanted]
    reports_dir = Path(args.reports_dir)
    previous = sorted(reports_dir.glob("EVAL-*.json"))

    def progress(r) -> None:
        mark = "PASS" if r.passed else "FAIL"
        print(
            f"  {r.case_id} {mark} {r.status:13} {r.duration_ms:>6}ms tools={r.model_tools} "
            f"{'; '.join(r.notes)}",
            flush=True,
        )

    with tempfile.TemporaryDirectory(prefix="payops-eval-") as work:
        report = ev.run_evaluation(
            settings,
            cases,
            work_dir=Path(work),
            dataset_sha256=ev.dataset_hash(dataset),
            progress=progress,
            partial=bool(args.cases),
        )
    conn = _open(settings)
    path = ev.save_report(conn, report, reports_dir)
    print(
        f"{report.evaluation_id} model={report.model_version} prompt={report.prompt_version} "
        f"tools={report.tools_version} release_gate={'PASSED' if report.passed else 'FAILED'}"
    )
    for name, value in report.metrics.items():
        print(f"  {name:30} {value}")
    print(f"report: {path}")
    if previous:
        cmp = ev.compare_reports(ev.load_report(previous[-1]), report)
        print(
            f"compared with {cmp.baseline_id}: regressions={cmp.regressions} "
            f"newly_failing={cmp.newly_failing} newly_passing={cmp.newly_passing}"
        )
    return 0 if report.passed else 5


def cmd_compare(settings: Settings, args: argparse.Namespace) -> int:
    from payops_sentinel.operations import evaluations as ev

    cmp = ev.compare_reports(
        ev.load_report(Path(args.baseline)), ev.load_report(Path(args.candidate))
    )
    print(json.dumps(cmp.model_dump(), indent=2))
    return 1 if cmp.regressions or cmp.newly_failing else 0


def cmd_backup(settings: Settings, args: argparse.Namespace) -> int:
    from payops_sentinel.storage.backup import backup_database, verify_database

    dest = backup_database(settings.db_path, Path(args.destination))
    check = verify_database(dest)
    print(json.dumps(check.model_dump(), indent=2))
    print(f"backup {'VERIFIED' if check.healthy else 'FAILED VERIFICATION'}: {dest}")
    return 0 if check.healthy else 6


def cmd_verify_db(settings: Settings, args: argparse.Namespace) -> int:
    from payops_sentinel.storage.backup import verify_database

    check = verify_database(Path(args.path) if args.path else settings.db_path)
    print(json.dumps(check.model_dump(), indent=2))
    print("HEALTHY" if check.healthy else "UNHEALTHY")
    return 0 if check.healthy else 6


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
    p_inv = sub.add_parser("investigate", help="run the AI agent on one exception")
    p_inv.add_argument("exception_id")
    p_eval = sub.add_parser("evaluate", help="run the golden-case agent evaluation (local model)")
    p_eval.add_argument("--cases", help="comma-separated case ids (default: all)")
    p_eval.add_argument("--reports-dir", default=str(PROJECT_ROOT / "evals" / "reports"))
    p_cmp = sub.add_parser("eval-compare", help="compare two evaluation reports")
    p_cmp.add_argument("baseline")
    p_cmp.add_argument("candidate")
    p_backup = sub.add_parser("backup", help="online SQLite backup + verification of the copy")
    p_backup.add_argument("destination")
    p_verify = sub.add_parser("verify-db", help="integrity, schema and audit-chain check")
    p_verify.add_argument("path", nargs="?")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings(args.config)
    handlers = {
        "generate-data": cmd_generate,
        "init": cmd_init,
        "ingest": cmd_ingest,
        "show": cmd_show,
        "investigate": cmd_investigate,
        "evaluate": cmd_evaluate,
        "eval-compare": cmd_compare,
        "backup": cmd_backup,
        "verify-db": cmd_verify_db,
    }
    return handlers[args.command](settings, args)


if __name__ == "__main__":
    raise SystemExit(main())
