"""RED tests for two lane-audit defects (hermes:project-controller, 2026-11-04).

1. D-CLAIMLOCK - release_stale_claims must decide "is this worker mine?" on worker_pid +
   started_at, NOT on claim_lock's host prefix. claim_lock records the HOST pid while the
   worker is a separate pid, so a live worker whose lock did not carry our prefix used to be
   terminated and reclaimed mid-work.

2. D-ONE-LINE-WRITE - the CYCLE_GUARD park path must write tasks.last_failure_error, because
   that is the field the board displays. block_task only writes result/summary, which is why
   8 of 16 parked cards showed no reason at all.

Both run against a COPY of the live board, so the real queue is never touched.
"""
import importlib.util
import os
import shutil
import sqlite3
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_DB = os.path.expanduser("~/.hermes/kanban.db")
PLUGIN = os.environ.get("REPL_PLUGIN") or os.path.expanduser(
    "~/.hermes/plugins/kanban-replenisher/__init__.py")


def _load_kanban_db():
    sys.path.insert(0, REPO)
    from hermes_cli import kanban_db
    return kanban_db


def _load_plugin():
    spec = importlib.util.spec_from_file_location("repl_under_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fresh_board(tmp_path):
    """Scratch board built from the product's own schema - never the real queue."""
    kb = _load_kanban_db()
    conn = sqlite3.connect(str(tmp_path / "board.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(kb.SCHEMA_SQL)
    conn.commit()
    return conn


def _insert_task(conn, **fields):
    """Insert into tasks, auto-filling any NOT NULL column the caller did not supply.

    The board schema has grown a lot of NOT NULL bookkeeping columns; listing them in the test
    would break on the next migration. Introspect instead.
    """
    info = list(conn.execute("PRAGMA table_info(tasks)"))
    have = {r[1] for r in info}
    for r in info:
        name, notnull, default = r[1], r[2], r[4]
        if name in fields or not notnull or default not in (None, ""):
            continue
        low = name.lower()
        if "at" in low:
            fields[name] = _now()
        elif low in ("id", "title", "status", "assignee"):
            fields[name] = ""
        else:
            fields[name] = 0
    cols = [c for c in fields if c in have]
    conn.execute("INSERT INTO tasks (%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))),
                 tuple(fields[c] for c in cols))
    conn.commit()


def _now():
    return int(time.time())


def _insert_running(conn, tid, claim_lock, worker_pid, claim_expires, heartbeat):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    row = {"id": tid, "title": "claimlock probe", "status": "running", "assignee": "default",
           "created_at": _now(), "updated_at": _now()}
    if "claim_lock" in cols:
        row["claim_lock"] = claim_lock
    if "worker_pid" in cols:
        row["worker_pid"] = worker_pid
    if "worker_started_at" in cols:
        row["worker_started_at"] = _now()
    if "claim_expires" in cols:
        row["claim_expires"] = claim_expires
    if "last_heartbeat_at" in cols:
        row["last_heartbeat_at"] = heartbeat
    _insert_task(conn, **row)


def test_live_worker_with_foreign_claim_lock_is_not_reclaimed(tmp_path, monkeypatch):
    """A live worker must keep its claim even when claim_lock carries a foreign host prefix."""
    kb = _load_kanban_db()
    conn = _fresh_board(tmp_path)
    tid = "t_claimlock_probe"
    conn.execute("DELETE FROM tasks WHERE id=?", (tid,))
    _insert_running(
        conn, tid,
        claim_lock="SOMEOTHERHOST:424242",   # NOT our host prefix
        worker_pid=os.getpid(),              # genuinely alive
        claim_expires=_now() - 60,           # claim looks expired
        heartbeat=_now(),                    # fresh: not wedged
    )
    monkeypatch.setattr(kb, "_worker_alive", lambda pid, started_at=None: True)
    kb.release_stale_claims(conn)
    status = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"]
    conn.close()
    assert status == "running", (
        "defect reproduced: a live worker was reclaimed purely because claim_lock's host "
        "prefix did not match; identity must come from worker_pid + started_at"
    )


def test_cycle_guard_writes_last_failure_error(tmp_path):
    """Parking a card must record the reason in the field the board displays."""
    mod = _load_plugin()
    kb = _load_kanban_db()
    conn = _fresh_board(tmp_path)
    tid = "t_oneline_probe"
    conn.execute("DELETE FROM task_runs WHERE task_id=?", (tid,))
    conn.execute("DELETE FROM tasks WHERE id=?", (tid,))
    _insert_task(conn, id=tid, title="oneline probe", status="todo", assignee="default",
                 result="", last_failure_error=None)
    run_cols = {r[1] for r in conn.execute("PRAGMA table_info(task_runs)")}
    for _ in range(2):
        fields = {"task_id": tid, "status": "failed"}
        if "started_at" in run_cols:
            fields["started_at"] = _now()
        names = ",".join(fields)
        conn.execute("INSERT INTO task_runs (%s) VALUES (%s)" % (names, ",".join("?" * len(fields))),
                     tuple(fields.values()))
    conn.commit()

    mod.enforce_no_cycling(conn, kb=kb, max_failures=2)

    row = conn.execute("SELECT status, last_failure_error FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert row["status"] == "blocked", "guard did not park the card"
    assert row["last_failure_error"] and str(row["last_failure_error"]).startswith("CYCLE_GUARD"), (
        "D-ONE-LINE-WRITE reproduced: the card is parked but last_failure_error (the displayed "
        "field) is still empty, so nobody can see why"
    )


def test_dead_worker_error_carries_worker_log_path(tmp_path, monkeypatch):
    """D-WORKER-LOG: a short/dead run must still tell you where its log is.

    A non-zero short run used to produce ~40 chars of error with no pointer to the log, so the
    cause died with the card. The path must travel with the error even when the log is empty.
    """
    sys.path.insert(0, REPO)
    from hermes_cli import kanban_db_dispatch as dispatch

    monkeypatch.setattr(dispatch._kb, "read_worker_log", lambda *a, **k: "")
    monkeypatch.setattr(dispatch._kb, "worker_logs_dir", lambda board=None: tmp_path)

    out = dispatch._worker_final_output("t_probe", board=None)

    assert "t_probe.log" in out, (
        "D-WORKER-LOG reproduced: the dead-worker error carries no worker-log path, so the "
        "cause cannot be found after the card is parked"
    )
    assert out.startswith("[worker log:"), "hint should lead when there is no output to show"


def test_lane_at_cap_is_not_claimed_but_other_lane_is(tmp_path, monkeypatch):
    """D-CAPS: a lane at its running cap must not be claimed, while a free lane still is."""
    kb = _load_kanban_db()
    conn = _fresh_board(tmp_path)
    monkeypatch.setattr(kb, "_lane_caps", lambda: {"nous": 2, "mistral": 2})

    now = _now()
    for i in range(2):
        _insert_task(conn, id="t_busy_%d" % i, title="busy", status="running",
                     assignee="default", provider_override="nous",
                     claim_expires=now + 600, last_heartbeat_at=now)
    _insert_task(conn, id="t_blocked_lane", title="wants nous", status="ready",
                 assignee="default", provider_override="nous")
    _insert_task(conn, id="t_free_lane", title="wants mistral", status="ready",
                 assignee="default", provider_override="mistral")
    _insert_task(conn, id="t_uncapped", title="no lane", status="ready",
                 assignee="default", provider_override="some-other-lane")

    # the predicate itself: full lane reports capacity, free lane does not
    assert kb._lane_at_capacity(conn, "t_blocked_lane") == ("nous", 2, 2)
    assert kb._lane_at_capacity(conn, "t_free_lane") is None

    # and the integration point refuses the claim rather than letting the lane exceed its cap
    assert kb.claim_task(conn, "t_blocked_lane", claimer="probe") is None, (
        "D-CAPS reproduced: a card was claimed onto a lane already running at its cap"
    )
    # a lane absent from the caps file is never limited here (0 means 'queue nothing', not 'run
    # nothing'), so an 'unset' card can still be claimed and the board cannot jam
    assert kb._lane_at_capacity(conn, "t_uncapped") is None

    # liveness: the refusal must clear the moment the lane frees a slot, otherwise a card could sit
    # ready forever behind a full lane it can never enter
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = 't_busy_0'")
    conn.commit()
    assert kb._lane_at_capacity(conn, "t_blocked_lane") is None, (
        "capacity must clear as soon as a slot frees, or the card is stranded although the lane is free"
    )
    conn.close()
