"""Regression tests for transient block auto-re-arm in kanban dispatcher.

A card blocked with block_kind='transient' should be automatically re-armed to 'todo'
after a cooldown period, with bounded retries per card and per tick.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
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


# ---------------------------------------------------------------------------
# Transient block auto-re-arm tests
# ---------------------------------------------------------------------------


def test_transient_block_rearmed_after_cooldown(conn) -> None:
    """A transient block older than the cooldown is re-armed once and gains a transient_rearmed event."""
    # Set cooldown to 0 for immediate re-arm in tests
    import os
    os.environ["HERMES_KANBAN_TRANSIENT_REARM_COOLDOWN_SECONDS"] = "0"

    tid = kb.create_task(conn, title="transient test", assignee="default")
    kb.claim_task(conn, tid)
    # Block with transient kind
    assert kb.block_task(conn, tid, reason="transient flake", kind="transient")
    task = kb.get_task(conn, tid)
    assert task.status == "blocked"
    assert task.block_kind == "transient"
    assert task.block_recurrences == 1

    # Run dispatch tick - should re-arm
    result = kbd.dispatch_once(conn, max_spawn=10)
    # Task should be re-armed to todo
    task = kb.get_task(conn, tid)
    assert task.status == "todo"
    assert task.block_recurrences == 2  # Incremented on re-arm

    # Check for transient_rearmed event
    events = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? AND kind = 'transient_rearmed'",
        (tid,)
    ).fetchall()
    assert len(events) == 1
    import json
    payload = json.loads(events[0]["payload"])
    assert payload["round"] == 2
    assert payload["by"] == "dispatch-tick"


def test_fresh_transient_block_not_rearmed(conn) -> None:
    """A fresh transient block (within cooldown) is NOT re-armed."""
    # Set cooldown to 300s (default)
    import os
    os.environ["HERMES_KANBAN_TRANSIENT_REARM_COOLDOWN_SECONDS"] = "300"

    tid = kb.create_task(conn, title="fresh transient", assignee="default")
    kb.claim_task(conn, tid)
    assert kb.block_task(conn, tid, reason="just happened", kind="transient")

    # Run dispatch tick - should NOT re-arm (cooldown not elapsed)
    result = kbd.dispatch_once(conn, max_spawn=10)
    task = kb.get_task(conn, tid)
    assert task.status == "blocked"
    assert task.block_recurrences == 1  # Not incremented

    # No transient_rearmed event
    events = conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? AND kind = 'transient_rearmed'",
        (tid,)
    ).fetchall()
    assert len(events) == 0


def test_transient_block_max_rounds_exhausted(conn) -> None:
    """A card at 3 re-arm rounds is NOT re-armed and gains transient_rearm_exhausted event."""
    import os
    os.environ["HERMES_KANBAN_TRANSIENT_REARM_COOLDOWN_SECONDS"] = "0"

    tid = kb.create_task(conn, title="exhausted transient", assignee="default")
    kb.claim_task(conn, tid)
    assert kb.block_task(conn, tid, reason="flaky", kind="transient")
    # Manually set block_recurrences to 3 (exhausted)
    conn.execute("UPDATE tasks SET block_recurrences = 3 WHERE id = ?", (tid,))
    conn.commit()

    # Run dispatch tick - should NOT re-arm, should record exhaustion
    result = kbd.dispatch_once(conn, max_spawn=10)
    task = kb.get_task(conn, tid)
    assert task.status == "blocked"
    assert task.block_recurrences == 3

    # Check for transient_rearm_exhausted event
    events = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? AND kind = 'transient_rearm_exhausted'",
        (tid,)
    ).fetchall()
    assert len(events) == 1
    import json
    payload = json.loads(events[0]["payload"])
    assert payload["rounds"] == 3
    assert payload["by"] == "dispatch-tick"


def test_transient_block_unreadable_timestamp_fails_closed(conn) -> None:
    """A transient block with unreadable event timestamp is NOT re-armed (fail closed)."""
    import os
    os.environ["HERMES_KANBAN_TRANSIENT_REARM_COOLDOWN_SECONDS"] = "0"

    tid = kb.create_task(conn, title="bad timestamp", assignee="default")
    kb.claim_task(conn, tid)
    assert kb.block_task(conn, tid, reason="flaky", kind="transient")

    # Corrupt the created_at of the blocked event to be unreadable
    conn.execute(
        "UPDATE task_events SET created_at = 'not-a-timestamp' WHERE task_id = ? AND kind = 'blocked'",
        (tid,)
    )
    conn.commit()

    # Run dispatch tick - should NOT re-arm (fail closed)
    result = kbd.dispatch_once(conn, max_spawn=10)
    task = kb.get_task(conn, tid)
    assert task.status == "blocked"
    assert task.block_recurrences == 1

    # No transient_rearmed event
    events = conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? AND kind = 'transient_rearmed'",
        (tid,)
    ).fetchall()
    assert len(events) == 0


def test_transient_block_max_two_per_tick(conn) -> None:
    """At most 2 transient blocks are re-armed per tick."""
    import os
    os.environ["HERMES_KANBAN_TRANSIENT_REARM_COOLDOWN_SECONDS"] = "0"

    # Create 4 transient blocked tasks
    tids = []
    for i in range(4):
        tid = kb.create_task(conn, title=f"transient {i}", assignee="default")
        kb.claim_task(conn, tid)
        kb.block_task(conn, tid, reason=f"flake {i}", kind="transient")
        tids.append(tid)

    # Run dispatch tick - only 2 should be re-armed
    result = kbd.dispatch_once(conn, max_spawn=10)

    rearmed = 0
    for tid in tids:
        task = kb.get_task(conn, tid)
        if task.status in ("todo", "ready", "running"):  # re-armed tasks may be promoted and spawned
            rearmed += 1

    assert rearmed == 2, f"Expected 2 re-armed, got {rearmed}"

    # Run again - the other 2 should be re-armed
    result = kbd.dispatch_once(conn, max_spawn=10)
    rearmed = 0
    for tid in tids:
        task = kb.get_task(conn, tid)
        if task.status in ("todo", "ready", "running"):
            rearmed += 1
    assert rearmed == 4


def test_transient_block_parent_gating_still_applies(conn) -> None:
    """Re-armed transient block goes to 'todo' and is promoted by recompute_ready."""
    import os
    os.environ["HERMES_KANBAN_TRANSIENT_REARM_COOLDOWN_SECONDS"] = "0"

    # Task with no parents - re-arm should go to 'todo', then recompute_ready promotes to 'ready'
    tid = kb.create_task(conn, title="transient task", assignee="default")
    kb.claim_task(conn, tid)
    kb.block_task(conn, tid, reason="transient flake", kind="transient")

    # Run dispatch tick - task should be re-armed to 'todo'
    result = kbd.dispatch_once(conn, max_spawn=10)
    task = kb.get_task(conn, tid)
    assert task.status == "todo"

    # Run recompute_ready - task should promote to 'ready' (no parents blocking)
    promoted = kb.recompute_ready(conn)
    assert promoted == 1
    task = kb.get_task(conn, tid)
    assert task.status == "ready"


def test_non_transient_block_not_affected(conn) -> None:
    """Blocks with other kinds (dependency, needs_input, capability) are not auto-re-armed."""
    import os
    os.environ["HERMES_KANBAN_TRANSIENT_REARM_COOLDOWN_SECONDS"] = "0"

    for kind in ["dependency", "needs_input", "capability"]:
        tid = kb.create_task(conn, title=f"block {kind}", assignee="default")
        kb.claim_task(conn, tid)
        kb.block_task(conn, tid, reason="blocked", kind=kind)

    # Run dispatch tick
    result = kbd.dispatch_once(conn, max_spawn=10)

    # None should be re-armed
    for kind in ["dependency", "needs_input", "capability"]:
        task = conn.execute(
            "SELECT status FROM tasks WHERE title = ?", (f"block {kind}",)
        ).fetchone()
        assert task["status"] == "blocked", f"{kind} block should not be re-armed"


def test_transient_rearmed_event_increments_block_recurrences(conn) -> None:
    """Each transient_rearmed event increments block_recurrences, tracking rounds correctly."""
    import os
    os.environ["HERMES_KANBAN_TRANSIENT_REARM_COOLDOWN_SECONDS"] = "0"

    tid = kb.create_task(conn, title="round tracker", assignee="default")
    kb.claim_task(conn, tid)
    kb.block_task(conn, tid, reason="flaky", kind="transient")
    assert kb.get_task(conn, tid).block_recurrences == 1

    # First re-arm
    kbd.dispatch_once(conn, max_spawn=10)
    assert kb.get_task(conn, tid).block_recurrences == 2

    # Promote to ready (simulate recompute_ready)
    kb.recompute_ready(conn)

    # Block again (simulate worker hitting it again) - block_recurrences should increment
    kb.claim_task(conn, tid)
    kb.block_task(conn, tid, reason="flaky again", kind="transient")
    # After re-block with same kind, block_recurrences increments
    assert kb.get_task(conn, tid).block_recurrences == 3

    # Second re-arm would be round 3, but max is 3 so exhausted
    kbd.dispatch_once(conn, max_spawn=10)
    task = kb.get_task(conn, tid)
    assert task.status == "blocked"
    assert task.block_recurrences == 3

    # Exhaustion event should exist
    events = conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? AND kind = 'transient_rearm_exhausted'",
        (tid,)
    ).fetchall()
    assert len(events) == 1