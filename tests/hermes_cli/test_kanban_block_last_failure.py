"""A block reason must be persisted in tasks.last_failure_error atomically."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_ready_block_writes_last_failure_error_in_transition(isolated_db):
    reason = "worker halted: needs operator input"
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="block reason", assignee="worker")
        assert kb.block_task(conn, tid, reason=reason, kind="needs_input")
        row = conn.execute(
            "SELECT status, last_failure_error FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert row["status"] == "blocked"
        assert row["last_failure_error"] == reason


def test_running_block_writes_last_failure_error_in_transition(isolated_db):
    reason = "review refused: concrete reason"
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="running block reason", assignee="worker")
        kb.claim_task(conn, tid)
        assert kb.block_task(conn, tid, reason=reason, kind="needs_input")
        row = conn.execute(
            "SELECT status, last_failure_error FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert row["status"] == "blocked"
        assert row["last_failure_error"] == reason
