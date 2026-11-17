"""Backup and restore are tested, not assumed: the restored copy must verify."""

from __future__ import annotations

import pytest

from payops_sentinel.review import service
from payops_sentinel.storage.backup import backup_database, verify_database


def test_online_backup_restores_with_intact_audit_chain(conn, settings, pending_draft, tmp_path):
    service.approve_draft(
        conn,
        service.resolve_principal("reviewer.demo"),
        artifacts_dir=settings.artifacts_dir,
        **pending_draft,
    )
    backup = backup_database(settings.db_path, tmp_path / "backup" / "payops-backup.db")
    original, restored = verify_database(settings.db_path), verify_database(backup)
    assert restored.healthy and restored.integrity_ok and restored.audit_chain_valid
    assert restored.row_counts == original.row_counts
    assert restored.row_counts["approvals"] == 1 and restored.audit_events == 2


def test_backup_refuses_to_overwrite(conn, settings, tmp_path):
    target = tmp_path / "b.db"
    backup_database(settings.db_path, target)
    with pytest.raises(FileExistsError):
        backup_database(settings.db_path, target)


def test_tampered_backup_fails_verification(conn, settings, pending_draft, tmp_path):
    import sqlite3

    service.approve_draft(
        conn,
        service.resolve_principal("reviewer.demo"),
        artifacts_dir=settings.artifacts_dir,
        **pending_draft,
    )
    backup = backup_database(settings.db_path, tmp_path / "b.db")
    raw = sqlite3.connect(backup)
    raw.execute("DROP TRIGGER audit_events_no_update")
    raw.execute("UPDATE audit_events SET actor_id = 'someone-else'")
    raw.commit()
    raw.close()
    assert not verify_database(backup).healthy
