"""Every Streamlit page renders, and the demo workflow can be clicked through (no Ollama)."""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from payops_sentinel.config import PROJECT_ROOT

UI = PROJECT_ROOT / "src" / "payops_sentinel" / "ui"
PAGES = ["app.py", "pages/1_Batches.py", "pages/2_Exceptions.py", "pages/3_Governance.py"]


@pytest.fixture
def ui_config(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(
        (PROJECT_ROOT / "config.toml")
        .read_text()
        .replace('db_path = "data/payops.db"', f'db_path = "{tmp_path / "ui.db"}"')
        .replace('artifacts_dir = "data/artifacts"', f'artifacts_dir = "{tmp_path / "art"}"')
        .replace('dir = "data/synthetic"', f'dir = "{PROJECT_ROOT / "data" / "synthetic"}"')
        .replace('"governance/', f'"{PROJECT_ROOT / "governance"}/')
    )
    monkeypatch.setenv("PAYOPS_CONFIG", str(config))
    return tmp_path


def _run(page: str, **state) -> AppTest:
    at = AppTest.from_file(str(UI / page), default_timeout=30)
    for key, value in state.items():
        at.session_state[key] = value
    return at.run()


@pytest.mark.parametrize("page", PAGES)
def test_pages_render_on_empty_database(ui_config, page):
    at = _run(page)
    assert not at.exception, at.exception


def test_operator_processes_demo_batch_then_pages_render(ui_config):
    at = _run("pages/1_Batches.py", principal_id="operator.demo")
    next(b for b in at.button if b.label == "Process demo file with exceptions").click().run()
    assert not at.exception
    assert any("BATCH-000001 processed" in s.value for s in at.success)
    for page in PAGES[2:]:
        assert not _run(page, principal_id="reviewer.demo").exception


def test_viewer_upload_is_refused(ui_config):
    at = _run("pages/1_Batches.py", principal_id="viewer.demo")
    next(b for b in at.button if b.label == "Process clean demo file").click().run()
    assert any("cannot upload" in e.value for e in at.error)


def test_governance_shows_recorded_evaluations(ui_config):
    from payops_sentinel.storage import repositories as repo
    from payops_sentinel.storage.db import connect, initialize, transaction

    conn = connect(ui_config / "ui.db")
    initialize(conn)
    metrics = {
        "case_pass_rate": 0.5,
        "tool_selection_accuracy": 0.5,
        "schema_validity": 1.0,
        "evidence_coverage": 1.0,
        "latency_p95_ms": 61000.0,
        "tool_argument_accuracy": 1.0,
        "failed_model_tool_calls": 3.0,
        "critical_security_violations": 0.0,
    }
    with transaction(conn):
        repo.insert_evaluation_run(
            conn,
            {
                "evaluation_id": "EVAL-TEST",
                "model_version": "qwen3:8b@abc",
                "agent_version": "x",
                "prompt_version": "1.0.0",
                "tools_version": "1.0.0",
                "started_at": "2026-11-17",
                "completed_at": "2026-11-17",
                "metrics": metrics,
                "passed": False,
            },
        )
    conn.close()
    at = _run("pages/3_Governance.py")
    assert not at.exception
    assert any("EVAL-TEST" in m.value for m in at.markdown)
