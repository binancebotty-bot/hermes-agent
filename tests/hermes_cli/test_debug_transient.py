"""Debug test for transient re-arm."""

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


def test_debug_transient_rearm(conn) -> None:
    """Debug transient re-arm."""
    os.environ["HERMES_KANBAN_TRANSIENT_REARM_COOLDOWN_SECONDS"] = "0"

    tids = []
    for i in range(4):
        tid = kb.create_task(conn, title=f"transient {i}", assignee="default")
        kb.claim_task(conn, tid)
        kb.block_task(conn, tid, reason=f"flake {i}", kind="transient")
        tids.append(tid)

    print("After setup:")
    for tid in tids:
        task = kb.get_task(conn, tid)
        print(f"  {tid}: status={task.status}, block_kind={task.block_kind}, block_recurrences={task.block_recurrences}")

    # Tick 1
    result = kbd.dispatch_once(conn, max_spawn=10)
    print(f"Tick 1: reclaimed={result.reclaimed}, promoted={result.promoted}")

    print("After tick 1:")
    for tid in tids:
        task = kb.get_task(conn, tid)
        print(f"  {tid}: status={task.status}, block_kind={task.block_kind}, block_recurrences={task.block_recurrences}")

    # Tick 2
    result = kbd.dispatch_once(conn, max_spawn=10)
    print(f"Tick 2: reclaimed={result.reclaimed}, promoted={result.promoted}")

    print("After tick 2:")
    for tid in tids:
        task = kb.get_task(conn, tid)
        print(f"  {tid}: status={task.status}, block_kind={task.block_kind}, block_recurrences={task.block_recurrences}")