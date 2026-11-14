"""M3 R3/R4/R5: the admission cutoff must be the EARLIEST authoritative terminal transition.

T1-T5 and the discriminating T9 case. The fixture carries the block AND the later reap with an output
between them, because a cutoff derived from the LATEST terminal event admits exactly that output.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hermes_cli import kanban_db_dispatch as dispatch  # noqa: E402

TASK = "t_m3_cutoff"
RUN = 7


@pytest.fixture()
def board():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, current_run_id INTEGER, worker_pid INTEGER);
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, worker_pid INTEGER,
                                worker_started_at TEXT, claim_lock TEXT, ended_at INTEGER);
        CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER,
                                  kind TEXT, payload TEXT, created_at INTEGER);
        """
    )
    conn.execute("INSERT INTO tasks VALUES (?,?,?,?)", (TASK, "running", RUN, 100))
    conn.execute("INSERT INTO task_runs VALUES (?,?,?,?,?,?)", (RUN, TASK, 100, "fp-1", "HOST:1", 1000))
    conn.commit()
    return conn


def _event(conn, kind, payload=None, run_id=RUN):
    cur = conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
        (TASK, run_id, kind, json.dumps(payload or {}), 0),
    )
    conn.commit()
    return cur.lastrowid


def test_t1_no_terminal_event_admits(board):
    out = _event(board, "worker_result", {"exit_code": 0})
    assert dispatch.admission_cutoff(board, TASK, RUN) is None
    assert dispatch.admission_verdict(board, TASK, RUN, out) == (True, None)


def test_t2_cutoff_is_earliest_terminal_not_latest(board):
    _event(board, "worker_bound", {"pid": 100})
    blocked = _event(board, "blocked", {"reason": "operator"})
    _event(board, "terminal_worker_reaped", {"pid": 100})
    _event(board, "blocked", {"reason": "second observation"})
    assert dispatch.admission_cutoff(board, TASK, RUN) == blocked


def test_t3_reap_does_not_move_the_cutoff(board):
    _event(board, "worker_bound", {"pid": 100})
    blocked = _event(board, "blocked", {"reason": "operator"})
    _event(board, "terminal_worker_reaped", {"pid": 100, "terminated": True})
    assert dispatch.admission_cutoff(board, TASK, RUN) == blocked, (
        "terminal_worker_reaped must not count: a reap can only follow a block, so counting it moves "
        "the boundary later and admits output written on an already-blocked card"
    )


def test_t4_output_after_cutoff_is_inadmissible(board):
    _event(board, "worker_bound", {"pid": 100})
    _event(board, "blocked", {"reason": "operator"})
    late = _event(board, "worker_result", {"exit_code": 0})
    admissible, cutoff = dispatch.admission_verdict(board, TASK, RUN, late)
    assert admissible is False
    assert cutoff is not None


def test_t5_other_run_events_are_ignored(board):
    other = _event(board, "blocked", {"reason": "previous run"}, run_id=RUN + 1)
    assert other
    out = _event(board, "worker_result", {"exit_code": 0})
    assert dispatch.admission_verdict(board, TASK, RUN, out) == (True, None)


def test_t6_unreadable_position_fails_closed(board):
    _event(board, "blocked", {"reason": "operator"})
    assert dispatch.admission_verdict(board, TASK, RUN, None)[0] is False
    assert dispatch.admission_verdict(board, TASK, RUN, "not-an-id")[0] is False


def test_t9_discriminating_against_a_latest_terminal_reading(board):
    """T9: block, then an output, then a SECOND terminal event of an admitted kind.

    Two terminal events of the same admitted kind is what separates "earliest" from "latest": with only one
    blocked event a latest-in-kind rule is indistinguishable, so the later event is the reap AND a completion.
    """
    _event(board, "worker_bound", {"pid": 100})
    blocked = _event(board, "blocked", {"reason": "operator"})
    between = _event(board, "worker_result", {"exit_code": 0})
    _event(board, "terminal_worker_reaped", {"pid": 100, "terminated": True})
    completed = _event(board, "completed", {"note": "late summary written by a woken worker"})
    earliest = dispatch.admission_cutoff(board, TASK, RUN)
    latest_in_kinds = board.execute(
        "SELECT MAX(id) FROM task_events WHERE task_id=? AND run_id=? "
        "AND kind IN ('blocked','cancelled','completed','gave_up')",
        (TASK, RUN),
    ).fetchone()[0]
    assert earliest == blocked < between < latest_in_kinds == completed
    # the shipped rule refuses the interleaved output ...
    assert dispatch.admission_verdict(board, TASK, RUN, between)[0] is False
    # ... a latest-in-kind rule WOULD have admitted it (between <= latest_in_kinds), which is the defect
    assert between <= latest_in_kinds


def test_t10_late_result_is_not_read_as_this_attempts_outcome(board, tmp_path):
    """R3 read side: session_kanban.worker_result() must not serve a result written after the cutoff."""
    from gateway import session_kanban

    board.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (TASK, RUN, "worker_bound", json.dumps({"pid": 100, "claim_lock": "HOST:1"}), 0))
    board.commit()
    _event(board, "blocked", {"reason": "operator"})
    late = _event(board, "worker_result", {"exit_code": 0, "claim_lock": "HOST:1", "last_output": "late"})

    db = tmp_path / "board.db"
    disk = sqlite3.connect(str(db))
    board.backup(disk)
    disk.close()

    served = session_kanban.worker_result(str(db), {"run_id": RUN, "task_id": TASK, "claim_lock": "HOST:1"})
    assert served == {}, f"late output (event {late}) must not be served as the attempt's result"
