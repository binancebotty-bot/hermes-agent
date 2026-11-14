"""Free-lane dispatch: explicit-model refusal + provider cycling on refusal.

Measured context (2026-11-14 probe): the free lanes (nous / openrouter / gemini)
all answer HTTP 200 at concurrency 1 and 3; the 429s are transient per-model
upstream capacity ("This is not your API key's rate limit"). So the defect is
not allowance and not concurrency>2 — it is the dispatcher requeueing the SAME
lane after a refusal. These tests pin the two repairs:

1. a card that states no model is refused at dispatch (no silent paid fallback);
2. a refused lane is cycled to the NEXT free lane, never re-run, and the cycle
   reports exhaustion only after the whole list is tried.
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest


@pytest.fixture()
def free_lane_home(monkeypatch):
    """Fresh HERMES_HOME with a board, one profile, and the dispatcher importable."""
    test_home = tempfile.mkdtemp(prefix="kanban_free_lane_cycle_")
    for prof in ("alpha", "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
        with open(os.path.join(test_home, "profiles", prof, "config.yaml"), "w") as fh:
            fh.write("{}\n")
    monkeypatch.setenv("HERMES_HOME", test_home)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    from hermes_cli import kanban_db
    from hermes_cli import kanban_db_dispatch
    monkeypatch.setattr(kanban_db_dispatch, "_system_memory_sample", dict)
    kanban_db_dispatch._recent_worker_exits.clear()
    yield kanban_db
    kanban_db_dispatch._recent_worker_exits.clear()


def _fake_spawn(*args, **kwargs):
    return 12345


def _events(conn, task_id, kind):
    return [r[0] for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
        (task_id, kind)).fetchall()]


# ---------------------------------------------------------------------------
# 1. explicit-model refusal
# ---------------------------------------------------------------------------


def test_dispatch_refuses_card_with_no_model(free_lane_home):
    """A ready card that states no model must NOT spawn — it would inherit the
    paid profile default silently. It is deferred, not dropped."""
    kb = free_lane_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="no model", assignee="alpha")
    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)
    assert res.spawned == []
    assert tid in res.skipped_no_model


def test_dispatch_spawns_card_with_model(free_lane_home):
    """Control: a card that states a model still dispatches normally."""
    kb = free_lane_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="has model", assignee="alpha",
                             model_override="meituan/longcat-2.5:free", provider_override="nous")
    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)
    assert [s[0] for s in res.spawned] == [tid]
    assert getattr(res, "skipped_no_model", []) == []


def test_refusal_is_recorded_on_the_card(free_lane_home):
    """The refusal leaves a durable board event naming the reason."""
    kb = free_lane_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="no model", assignee="alpha")
    with kbc.connect_closing() as conn:
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        payloads = _events(conn, tid, "skipped_no_model")
    assert payloads, "refusal must be recorded on the card"
    assert "model" in payloads[-1]


# ---------------------------------------------------------------------------
# 2. provider cycling on refusal
# ---------------------------------------------------------------------------


def _dead_rate_limited_run(kb, conn, kbd, tid, worker_tag="w0"):
    """Simulate a worker that died on a quota/capacity wall (exit 75)."""
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, tid, claimer=f"{host}:{worker_tag}")
    pid = 70000 + abs(hash((tid, worker_tag))) % 1000
    conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
    conn.commit()
    kbd._record_worker_exit(pid, kbd._wait_status_from_returncode(kb.KANBAN_RATE_LIMIT_EXIT_CODE))
    return pid


def test_rate_limit_cycles_to_next_free_lane(free_lane_home):
    """A rate-limited worker on nous must requeue on the NEXT free lane, with
    the refusal and the cycle both recorded."""
    kb = free_lane_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="rl", assignee="alpha",
                             model_override="meituan/longcat-2.5:free", provider_override="nous")
    with kbc.connect_closing() as conn:
        _dead_rate_limited_run(kb, conn, kbd, tid)
        kbd.detect_crashed_workers(conn)
        row = conn.execute(
            "SELECT provider_override, model_override FROM tasks WHERE id=?", (tid,)).fetchone()
        provider, model = row[0], row[1]
        refused = _events(conn, tid, "provider_refused")
        cycled = _events(conn, tid, "provider_cycled")
    assert provider == "openrouter", f"expected cycle nous->openrouter, got {provider}"
    assert model == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert refused and '"nous"' in refused[-1]
    assert cycled and '"to": "openrouter"' in cycled[-1]


def test_cycle_never_reuses_a_refused_provider(free_lane_home):
    """Second refusal moves to gemini — the already-refused lanes are not retried."""
    kb = free_lane_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="rl2", assignee="alpha",
                             model_override="meituan/longcat-2.5:free", provider_override="nous")
    with kbc.connect_closing() as conn:
        _dead_rate_limited_run(kb, conn, kbd, tid, "w0")
        kbd.detect_crashed_workers(conn)
        _dead_rate_limited_run(kb, conn, kbd, tid, "w1")
        kbd.detect_crashed_workers(conn)
        row = conn.execute(
            "SELECT provider_override, model_override FROM tasks WHERE id=?", (tid,)).fetchone()
        provider, model = row[0], row[1]
    assert provider == "gemini", f"expected cycle nous->openrouter->gemini, got {provider}"
    assert model == "gemini-3.1-flash-lite"


def test_cycle_reports_exhausted_after_whole_list(free_lane_home):
    """When every free lane has been tried, the card is left in place and a
    provider_exhausted event is recorded — no silent same-lane requeue."""
    kb = free_lane_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="rl3", assignee="alpha",
                             model_override="meituan/longcat-2.5:free", provider_override="nous")
    with kbc.connect_closing() as conn:
        for i in range(3):
            _dead_rate_limited_run(kb, conn, kbd, tid, f"w{i}")
            kbd.detect_crashed_workers(conn)
        row = conn.execute(
            "SELECT provider_override FROM tasks WHERE id=?", (tid,)).fetchone()
        provider = row[0]
        exhausted = _events(conn, tid, "provider_exhausted")
    assert provider == "gemini", f"no further lane should be chosen, got {provider}"
    assert exhausted, "exhaustion must be reported on the card"


def test_cycle_skips_a_free_lane_that_is_at_its_concurrency_ceiling(free_lane_home):
    """The cycle honours the per-provider ceiling of 2: a lane already running
    2 workers is skipped in favour of the next one with headroom."""
    kb = free_lane_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        # Two openrouter workers already in flight -> openrouter is at its ceiling.
        for i in range(2):
            oid = kb.create_task(conn, title=f"or{i}", assignee="alpha",
                                 model_override="nvidia/nemotron-3-ultra-550b-a55b:free",
                                 provider_override="openrouter")
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (oid,))
        conn.commit()
        tid = kb.create_task(conn, title="rl4", assignee="alpha",
                             model_override="meituan/longcat-2.5:free", provider_override="nous")
    with kbc.connect_closing() as conn:
        _dead_rate_limited_run(kb, conn, kbd, tid)
        kbd.detect_crashed_workers(conn)
        row = conn.execute(
            "SELECT provider_override FROM tasks WHERE id=?", (tid,)).fetchone()
        provider = row[0]
    assert provider == "gemini", f"openrouter is at ceiling 2; expected gemini, got {provider}"


def test_non_refusal_crash_does_not_cycle(free_lane_home):
    """Control: an ordinary crash (no capacity/refusal signature) leaves the
    card's lane untouched."""
    kb = free_lane_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="plain", assignee="alpha",
                             model_override="meituan/longcat-2.5:free", provider_override="nous")
    with kbc.connect_closing() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, tid, claimer=f"{host}:w0")
        pid = 78901
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
        conn.commit()
        kbd._record_worker_exit(pid, kbd._wait_status_from_returncode(1))
        kbd.detect_crashed_workers(conn)
        row = conn.execute(
            "SELECT provider_override FROM tasks WHERE id=?", (tid,)).fetchone()
        provider = row[0]
    assert provider == "nous"
    with kbc.connect_closing() as conn:
        assert _events(conn, tid, "provider_cycled") == []
