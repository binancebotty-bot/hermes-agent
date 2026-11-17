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


@pytest.fixture(autouse=True)
def _isolate_os_reaper(monkeypatch):
    """The repo test suite leaks a `_FakePopen` into `subprocess.Popen`; the reclaim path then raises
    AttributeError inside `reap_worker_zombies`. Admission never depends on OS zombie reaping, so it is
    stubbed here for every test in this file - otherwise these tests fail for a reason that is not the gate.
    """
    monkeypatch.setattr(kbd, "reap_worker_zombies", list)


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


def test_cooldown_tiers_default_on_and_switchable(kanban_home, monkeypatch):
    """P0-B phase 2 flipped the default: the ladder is the safe default and an explicit switch resumes
    the old flat cooldown. A fix that hides behind an opt-in flag is not a fix."""
    monkeypatch.delenv(kbd._COOLDOWN_TIERS_ENV, raising=False)
    assert kbd._cooldown_tiers_enabled() is True
    monkeypatch.setenv(kbd._COOLDOWN_TIERS_ENV, "0")
    assert kbd._cooldown_tiers_enabled() is False


# ---------------------------------------------------------------------------
# SEND-BACK 2026-11-17T2232Z: promotion and MANUAL claims must also be gated.
# The first cut checked the sentinel at the start of the tick, but the reclaim phase promotes todo -> ready
# BEFORE that check, and claim_task/claim_review_task are reachable directly (CLI, dashboard). The guard
# therefore moved into the claim primitives, where every -> running transition passes, and promotion is
# skipped while the hold is engaged.
# ---------------------------------------------------------------------------

def _parent_done(conn, parent_id):
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent_id,))
    conn.commit()


def test_hold_blocks_todo_to_ready_promotion(kanban_home, all_assignees_spawnable):
    """A child whose parent is complete must NOT be promoted while the hold is engaged."""
    sentinel = kanban_home / kbd.ADMISSION_HOLD_NAME
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="default")
        # Created while the parent is OPEN, so the child legitimately starts in todo; the parent is completed
        # afterwards, which is exactly the state the dispatcher would promote on its next tick.
        child = kb.create_task(conn, title="child", assignee="default", parents=[parent])
        _parent_done(conn, parent)
        sentinel.write_text("{}", encoding="utf-8")
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
        status = _status(conn, child)
        promoted = list(getattr(res, "promoted", []) or [])
    assert status == "todo", "promotion is a queue step toward admission; the hold must stop it"
    assert child not in promoted, promoted


def test_manual_claim_is_refused_while_held(kanban_home, all_assignees_spawnable):
    """The CLI/dashboard path: claim_task directly must be refused, with no run and no running row."""
    sentinel = kanban_home / kbd.ADMISSION_HOLD_NAME
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="manual claim", assignee="default")
        runs_before = conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0]
        sentinel.write_text("{}", encoding="utf-8")
        claimed = kb.claim_task(conn, tid)
        status = _status(conn, tid)
        runs_after = conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0]
        events = [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id = ?", (tid,)).fetchall()]
    assert claimed is None, "a held board must not hand out a claim"
    assert status == "ready", status
    assert runs_after == runs_before, "no run may be opened by a refused claim"
    assert "claim_rejected" in events, events


def test_hold_engaged_mid_tick_still_prevents_the_second_claim(kanban_home, all_assignees_spawnable):
    """RACE: the tick has already read the queue; a hold engaged during the first spawn must stop the next claim.

    This is the atomicity the reviewer asked for: the guard is evaluated per claim, inside the claim transaction,
    not once at the top of the tick."""
    sentinel = kanban_home / kbd.ADMISSION_HOLD_NAME
    calls = []

    def _spawn(task, workspace, board=None):
        calls.append(getattr(task, "id", str(task)))
        sentinel.write_text("{}", encoding="utf-8")  # engaged after the tick started, before the second claim
        return 4242

    with kbc.connect() as conn:
        first = kb.create_task(conn, title="first", assignee="default", priority=10)
        second = kb.create_task(conn, title="second", assignee="default", priority=1)
        kbd.dispatch_once(conn, spawn_fn=_spawn)
        s1, s2 = _status(conn, first), _status(conn, second)
    assert calls == [first], calls
    assert s1 == "running" and s2 == "ready", (s1, s2)


def test_reclaim_still_runs_while_held(kanban_home, all_assignees_spawnable, monkeypatch):
    """Positive control: a dead worker is still reclaimed during a hold - the hold is not a freeze.

    The OS zombie reaper is stubbed out: it is unrelated to admission, and other modules in a wide run
    monkeypatch the process layer under it, which would make this test flaky for reasons that are not the gate.
    """
    monkeypatch.setattr(kbd, "reap_worker_zombies", list)
    sentinel = kanban_home / kbd.ADMISSION_HOLD_NAME
    sentinel.write_text("{}", encoding="utf-8")
    now = int(time.time())
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="dead worker", assignee="default")
        conn.execute(
            "UPDATE tasks SET status = 'running', worker_pid = 31337, worker_started_at = ?, "
            "last_heartbeat_at = ?, claim_lock = 'stale', claim_expires = ? WHERE id = ?",
            (now - 7200, now - 7200, now - 3600, tid),
        )
        conn.commit()
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242, stale_timeout_seconds=60)
        status = _status(conn, tid)
    assert res.admission_held is True
    assert status != "running", "a worker with an expired claim must be reclaimed even under hold"


def test_promotion_is_blocked_at_its_own_boundary(kanban_home, all_assignees_spawnable):
    """THIRD CALLER: `hermes kanban list` calls kb.recompute_ready() itself, outside any dispatcher tick.

    So the barrier has to live inside the promotion primitive, not at one dispatcher call site.
    """
    sentinel = kanban_home / kbd.ADMISSION_HOLD_NAME
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="default")
        child = kb.create_task(conn, title="child", assignee="default", parents=[parent])
        _parent_done(conn, parent)
        sentinel.write_text("{}", encoding="utf-8")
        promoted = kb.recompute_ready(conn)
        status = _status(conn, child)
    assert promoted == 0, promoted
    assert status == "todo", status


def test_direct_review_claim_is_refused_while_held(kanban_home, all_assignees_spawnable):
    """Coverage gap named by the reviewer: the review lane has its own claim primitive, guarded in
    production but not exercised here. review -> running must be refused under the hold, with no new run."""
    sentinel = kanban_home / kbd.ADMISSION_HOLD_NAME
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="review me", assignee="default")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        conn.commit()
        runs_before = conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0]
        sentinel.write_text("{}", encoding="utf-8")
        claimed = kb.claim_review_task(conn, tid)
        status = _status(conn, tid)
        runs_after = conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0]
    assert claimed is None, "a held board must not hand out a review claim"
    assert status == "review", status
    assert runs_after == runs_before, "a refused review claim must open no run"


# ---------------------------------------------------------------------------
# P0-B PHASE 2 (operator-authorised, 2026-11-17T2307Z): the 429 spawn storm.
# The admission gate stops NEW work on command. This phase bounds the retry churn
# of work a provider is already refusing: a per-card ladder that is ON by default,
# and a provider circuit that defers instead of retrying.
# ---------------------------------------------------------------------------

def _seed_rate_limit_run(conn, task_id, ts, started=None):
    """One quota-wall run. Provider is NOT a run column in this schema - attribution is by card pin."""
    conn.execute(
        # status AND outcome: the respawn guard keys on outcome, the cooldown ladder counts status
        "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
        "VALUES (?, 'rate_limited', 'rate_limited', ?, ?)",
        (task_id, ts if started is None else started, ts),
    )


class _Clock:
    """Deterministic clock: the storm simulation must not depend on wall time."""

    def __init__(self, start):
        self.now = int(start)

    def time(self):
        return self.now


def test_adaptive_cooldown_is_the_default_not_an_inert_flag(kanban_home, monkeypatch):
    """A fix that only works when someone remembers a flag is not a fix."""
    monkeypatch.delenv(kbd._COOLDOWN_TIERS_ENV, raising=False)
    assert kbd._cooldown_tiers_enabled() is True
    monkeypatch.setenv(kbd._COOLDOWN_TIERS_ENV, "0")
    assert kbd._cooldown_tiers_enabled() is False


def test_provider_circuit_defers_a_pinned_card_with_zero_spawns(kanban_home, all_assignees_spawnable):
    """Eight quota walls from one provider in the window: its card waits, nothing spawns."""
    now = int(time.time())
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="pinned", assignee="default", model_override="test-model", provider_override="prov-a")
        for _ in range(kbd.PROVIDER_QUOTA_THRESHOLD):
            _seed_rate_limit_run(conn, tid, now)
        conn.commit()
        calls = []
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: (calls.append(1) or 4242))
        status = _status(conn, tid)
    assert calls == [], calls
    assert tid in res.provider_exhausted, res.provider_exhausted
    assert status == "ready", status


def test_provider_circuit_recovers_once_the_window_ages(kanban_home, all_assignees_spawnable):
    """No state to clear: the same rows stop counting when they age out."""
    # past BOTH windows: the 15-minute provider circuit and the 6-hour ladder. A younger run would still be
    # legitimately cooling down, which is the fix working, not failing.
    old = int(time.time()) - kbd._RATE_LIMIT_WINDOW_SECONDS - 600
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="recovered", assignee="default", model_override="test-model", provider_override="prov-b")
        for _ in range(kbd.PROVIDER_QUOTA_THRESHOLD):
            _seed_rate_limit_run(conn, tid, old)
        conn.commit()
        calls = []
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: (calls.append(1) or 4242))
    assert len(calls) == 1, calls
    assert tid not in res.provider_exhausted


def test_the_24_card_storm_is_bounded_by_the_ladder(kanban_home, all_assignees_spawnable, monkeypatch):
    """Deterministic simulation: 24 cards all refused with 429, compared with the ladder off.

    Every attempt inserts a rate_limited run, exactly as a worker exit 75 does, and the guard is asked
    again on the next tick. The count of attempts IS the churn under test.
    """
    start = 1_700_000_000

    def _run(tiers_on, ticks, tick_seconds):
        monkeypatch.setenv(kbd._COOLDOWN_TIERS_ENV, "1" if tiers_on else "0")
        clock = _Clock(start)
        monkeypatch.setattr(kbd, "time", clock)
        with kbc.connect() as conn:
            ids = [kb.create_task(conn, title=f"storm {i}", assignee="default", model_override="test-model", provider_override=f"p{i % 4}")
                   for i in range(24)]
            attempts = 0
            for _ in range(ticks):
                clock.now += tick_seconds
                for tid in ids:
                    if kbd.check_respawn_guard(conn, tid) is None:
                        attempts += 1
                        _seed_rate_limit_run(conn, tid, clock.now)
                conn.commit()
        return attempts

    churn_10min = _run(tiers_on=True, ticks=30, tick_seconds=20)      # 10 minutes
    churn_10min_off = _run(tiers_on=False, ticks=30, tick_seconds=20)
    churn_7h = _run(tiers_on=True, ticks=420, tick_seconds=60)        # 7 hours
    churn_7h_off = _run(tiers_on=False, ticks=420, tick_seconds=60)

    assert churn_10min < churn_10min_off, (churn_10min, churn_10min_off)
    assert churn_7h < churn_7h_off, (churn_7h, churn_7h_off)
    # and the ladder must be materially quieter, not marginally
    assert churn_7h <= churn_7h_off / 4, (churn_7h, churn_7h_off)


def test_circuit_counts_are_keyed_on_run_END_not_start(kanban_home, all_assignees_spawnable):
    """THE DEFECT THE REVIEWER FOUND: a refusal is recorded when the run ENDS.

    Eight workers that STARTED 16 minutes ago and were refused a minute ago must trip the circuit. Keying the
    window on started_at missed them entirely, so an exhausted provider kept admitting new work."""
    now = int(time.time())
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="slow refusals", assignee="default",
                             model_override="m", provider_override="prov-slow")
        for _ in range(kbd.PROVIDER_QUOTA_THRESHOLD):
            _seed_rate_limit_run(conn, tid, now, started=now - 16 * 60)
        conn.commit()
        calls = []
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: (calls.append(1) or 4242))
    assert calls == [], "long-running refusals must still trip the circuit"
    assert tid in res.provider_exhausted, res.provider_exhausted


def test_review_lane_is_gated_by_the_provider_circuit_too(kanban_home, all_assignees_spawnable):
    """The circuit sits on the shared ready/review admission path, not the ready loop."""
    now = int(time.time())
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="pinned review", assignee="default",
                             model_override="m", provider_override="prov-rev")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        for _ in range(kbd.PROVIDER_QUOTA_THRESHOLD):
            _seed_rate_limit_run(conn, tid, now)
        conn.commit()
        calls = []
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: (calls.append(1) or 4242))
        status = _status(conn, tid)
    assert calls == [], "a pinned REVIEW card must not spawn while its provider is exhausted"
    assert tid in res.provider_exhausted, res.provider_exhausted
    assert status == "review", status


def test_circuit_recovers_when_END_times_age_out(kanban_home, all_assignees_spawnable):
    """No state to clear: rows whose END is outside the window stop counting."""
    old = int(time.time()) - kbd._RATE_LIMIT_WINDOW_SECONDS - 600
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="aged end", assignee="default",
                             model_override="m", provider_override="prov-c")
        for _ in range(kbd.PROVIDER_QUOTA_THRESHOLD):
            _seed_rate_limit_run(conn, tid, old, started=old - 30)
        conn.commit()
        calls = []
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: (calls.append(1) or 4242))
    assert len(calls) == 1, calls
    assert tid not in res.provider_exhausted
