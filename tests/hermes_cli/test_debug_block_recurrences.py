"""Debug test for block_recurrences increment."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def test_debug_block_recurrences(conn) -> None:
    """Debug block_recurrences increment."""
    os.environ["HERMES_KANBAN_TRANSIENT_REARM_COOLDOWN_SECONDS"] = "0"

    tid = kb.create_task(conn, title="round tracker", assignee="default")
    kb.claim_task(conn, tid)
    kb.block_task(conn, tid, reason="flaky", kind="transient")
    task = kb.get_task(conn, tid)
    print(f"After initial block: status={task.status}, block_kind={task.block_kind}, block_recurrences={task.block_recurrences}")
    assert task.block_recurrences == 1

    # First re-arm
    kbd.dispatch_once(conn, max_spawn=10)
    task = kb.get_task(conn, tid)
    print(f"After re-arm: status={task.status}, block_kind={task.block_kind}, block_recurrences={task.block_recurrences}")
    assert task.block_recurrences == 2

    # Promote to ready (simulate recompute_ready)
    promoted = kb.recompute_ready(conn)
    print(f"After recompute_ready: promoted={promoted}")
    task = kb.get_task(conn, tid)
    print(f"After promote: status={task.status}, block_kind={task.block_kind}, block_recurrences={task.block_recurrences}")

    # Block again (simulate worker hitting it again)
    kb.claim_task(conn, tid)
    task = kb.get_task(conn, tid)
    print(f"After claim: status={task.status}, block_kind={task.block_kind}, block_recurrences={task.block_recurrences}")
    
    kb.block_task(conn, tid, reason="flaky again", kind="transient")
    task = kb.get_task(conn, tid)
    print(f"After re-block: status={task.status}, block_kind={task.block_kind}, block_recurrences={task.block_recurrences}")
    # After re-block with same kind, block_recurrences increments
    assert task.block_recurrences == 3