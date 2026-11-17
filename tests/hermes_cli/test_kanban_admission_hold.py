"""Admission hold and the opt-in adaptive cooldown (P0-B, offline).

The operator needs a way to stop NEW task admissions without killing anything already running and
without touching sends, cron or the operator's own Desktop turns. The global ESTOP sentinel cannot
serve that purpose: its own help text says it also holds gateway turns, including Desktop.

This file pins the two mechanisms added here:

1. ``KANBAN_ADMISSION_HOLD`` - a reversible sentinel checked inside one dispatcher tick. Reclaim of
   dead workers still runs; promotion, claims and spawns do not. Removing the file resumes.
2. ``HERMES_KANBAN_COOLDOWN_TIERS`` - an OPT-IN, stateless adaptive cooldown derived from a card's own
   ``rate_limited`` history, so a card hammering the same quota wall backs off instead of retrying once
   a minute forever. Off by default: unset, behaviour is exactly today's env-configured cooldown.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _spawner():
    calls = []

    def _spawn(task, workspace, board=None):
        calls.append(getattr(task, "id", str(task)))
        return 4242

    return _spawn, calls


def _status(conn, tid):
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    return row[0]


def test_hold_blocks_new_admission(kanban_home, all_assignees_spawnable):
    """With the sentinel present nothing is admitted, and the card is not mutated."""
    (kanban_home / kbd.ADMISSION_HOLD_NAME).write_text("{}", encoding="utf-8")
    spawn, calls = _spawner()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="held card", assignee="default")
        res = kbd.dispatch_once(conn, spawn_fn=spawn)
        status = _status(conn, tid)
    assert res.admission_held is True, res
    assert calls == [], "the hold must not spawn anything"
    assert status == "ready", "a held card keeps its place in the queue"


def test_without_the_hold_admission_proceeds(kanban_home, all_assignees_spawnable):
    """Absence of the sentinel must restore normal admission exactly."""
    spawn, calls = _spawner()
    with kbc.connect() as conn:
        kb.create_task(conn, title="free card", assignee="default")
        res = kbd.dispatch_once(conn, spawn_fn=spawn)
    assert res.admission_held is False, res
    assert len(calls) == 1, calls


def test_hold_preserves_a_running_task(kanban_home, all_assignees_spawnable):
    """In-flight work is never touched: the running row keeps its status and its worker pid."""
    (kanban_home / kbd.ADMISSION_HOLD_NAME).write_text("{}", encoding="utf-8")
    spawn, calls = _spawner()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="in flight", assignee="default")
        # A LEGITIMATE in-flight row: a live claim with a future expiry and a fresh heartbeat, so the
        # reclaim phase has no reason to touch it and the test measures the HOLD, not the reaper.
        now = int(time.time())
        conn.execute(
            "UPDATE tasks SET status = 'running', worker_pid = 999, worker_started_at = ?, "
            "last_heartbeat_at = ?, claim_lock = 'test-lock', claim_expires = ? WHERE id = ?",
            (now, now, now + 3600, tid),
        )
        conn.commit()
        res = kbd.dispatch_once(conn, spawn_fn=spawn)
        row = conn.execute("SELECT status, worker_pid FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert res.admission_held is True
    assert calls == []
    assert (row[0], row[1]) == ("running", 999), row


def test_removing_the_hold_resumes_admission(kanban_home, all_assignees_spawnable):
    """Reversibility: delete the sentinel and the very next tick admits again, with no other state to unwind."""
    sentinel = kanban_home / kbd.ADMISSION_HOLD_NAME
    sentinel.write_text("{}", encoding="utf-8")
    spawn, calls = _spawner()
    with kbc.connect() as conn:
        kb.create_task(conn, title="resumes", assignee="default")
        first = kbd.dispatch_once(conn, spawn_fn=spawn)
        assert first.admission_held is True and calls == []
        sentinel.unlink()
        second = kbd.dispatch_once(conn, spawn_fn=spawn)
    assert second.admission_held is False
    assert len(calls) == 1, calls


def _seed_rate_limited(conn, tid, count, age_seconds=0):
    now = int(time.time()) - age_seconds
    for _ in range(count):
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'rate_limited', 'rate_limited', ?, ?)",
            (tid, now, now),
        )
    conn.commit()


@pytest.mark.parametrize("n,expected", [(1, 600), (3, 600), (4, 1800), (7, 1800), (8, 7200), (20, 7200)])
def test_cooldown_tiers_are_derived_from_history(kanban_home, n, expected):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title=f"tier {n}", assignee="default")
        _seed_rate_limited(conn, tid, n)
        assert kbd._rate_limit_cooldown_seconds(conn, tid) == expected


def test_cooldown_tiers_ignore_runs_outside_the_window(kanban_home):
    """The ladder is stateless: old runs age out, which is how a recovered card clears itself."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="aged", assignee="default")
        _seed_rate_limited(conn, tid, 9, age_seconds=kbd._RATE_LIMIT_WINDOW_SECONDS + 600)
        assert kbd._rate_limit_cooldown_seconds(conn, tid) == 0


def test_cooldown_tiers_are_off_by_default(kanban_home, monkeypatch):
    """Unset flag -> the env-configured cooldown governs, i.e. today's behaviour is preserved."""
    monkeypatch.delenv(kbd._COOLDOWN_TIERS_ENV, raising=False)
    assert kbd._cooldown_tiers_enabled() is False
    monkeypatch.setenv(kbd._COOLDOWN_TIERS_ENV, "1")
    assert kbd._cooldown_tiers_enabled() is True
