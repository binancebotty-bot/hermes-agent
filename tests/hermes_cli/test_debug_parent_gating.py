"""Debug test for parent gating."""

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


def test_debug_parent_gating(conn) -> None:
    """Debug parent gating."""
    os.environ["HERMES_KANBAN_TRANSIENT_REARM_COOLDOWN_SECONDS"] = "0"

    # Parent without assignee so it won't be auto-spawned by dispatcher
    parent = kb.create_task(conn, title="parent")
    child = kb.create_task(conn, title="child", assignee="default", parents=[parent])

    print(f"After create: parent={kb.get_task(conn, parent).status}, child={kb.get_task(conn, child).status}")

    kb.claim_task(conn, child)
    kb.block_task(conn, child, reason="transient flake", kind="transient")

    print(f"After block: child={kb.get_task(conn, child).status}")

    # Run dispatch tick - child should go to 'todo' (not 'ready') because parent not done
    result = kbd.dispatch_once(conn, max_spawn=10)
    print(f"Dispatch result: promoted={result.promoted}")
    task = kb.get_task(conn, child)
    print(f"After dispatch: child={task.status}, block_recurrences={task.block_recurrences}")
    assert task.status == "todo"

    # Complete parent (no assignee, so no live claim issue)
    kb.claim_task(conn, parent)
    print(f"After claim parent: parent={kb.get_task(conn, parent).status}")
    kb.complete_task(conn, parent, result="done")
    print(f"After complete parent: parent={kb.get_task(conn, parent).status}")

    # Run recompute_ready - child should now promote to 'ready'
    promoted = kb.recompute_ready(conn)
    print(f"Recompute promoted: {promoted}")
    task = kb.get_task(conn, child)
    print(f"After recompute: child={task.status}")
    assert promoted == 1
    assert task.status == "ready"