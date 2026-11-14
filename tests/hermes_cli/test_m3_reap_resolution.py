"""M3 R1/R2/R6: the terminal-worker reaper must be run-scoped, bound-first, revalidated and fail-closed.

T7-T9: run-scoped selection, per-pid outcomes, PID_REUSED with both fingerprints, and the rule that an
UNRESOLVED outcome prohibits the optimistic clear.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hermes_cli import kanban_db_dispatch as dispatch  # noqa: E402

TASK = "t_m3_reap"
RUN = 11
NEXT_RUN = 12
HOST = "HOST:"


@pytest.fixture()
def board(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, current_run_id INTEGER, worker_pid INTEGER);
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, worker_pid INTEGER,
                                worker_started_at TEXT, claim_lock TEXT, ended_at INTEGER, status TEXT);
        CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER,
                                  kind TEXT, payload TEXT, created_at INTEGER);
        """
    )
    # the requeue already moved the card to the NEXT run: a current_run_id join would pick run 12's worker
    conn.execute("INSERT INTO tasks VALUES (?,?,?,?)", (TASK, "running", NEXT_RUN, 909))
    conn.execute("INSERT INTO task_runs VALUES (?,?,?,?,?,?,?)", (RUN, TASK, 100, "fp-1", HOST + "1", 1000, "blocked"))
    conn.execute("INSERT INTO task_runs VALUES (?,?,?,?,?,?,?)", (NEXT_RUN, TASK, 909, "fp-9", HOST + "2", None, "running"))
    for run_id, pid, lock in ((RUN, 100, HOST + "1"), (NEXT_RUN, 909, HOST + "2")):
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
            (TASK, run_id, "worker_bound", json.dumps({"pid": pid, "claim_lock": lock}), 0),
        )
    conn.commit()

    monkeypatch.setattr(dispatch._kb, "_host_prefix", lambda: HOST)
    monkeypatch.setattr(dispatch._kb, "_pid_alive", lambda pid: pid in _state["alive"])
    monkeypatch.setattr(dispatch, "_pid_recycled", lambda pid, fp: pid in _state["recycled"])
    monkeypatch.setattr(dispatch, "_process_fingerprint", lambda pid: "fp-current")
    monkeypatch.setattr(dispatch, "_terminate_reclaimed_worker",
                        lambda pid, lock, signal_fn=None, started_at=None: _state["terminations"].append(pid) or
                        {"prev_pid": pid, "terminated": _state["terminates"], "termination_attempted": True,
                         "sigkill": False, "host_local": True})
    _state.clear()
    _state.update({"alive": set(), "recycled": set(), "terminations": [], "terminates": True})
    return conn


_state = {}


def _row(conn, run_id=RUN):
    return conn.execute(
        "SELECT id, task_id, worker_pid, worker_started_at, claim_lock FROM task_runs WHERE id=?", (run_id,)
    ).fetchone()


def _events(conn, kind):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (TASK, kind))]


def test_t7_selection_is_run_scoped(board):
    pids = [pid for pid, _ in dispatch.run_scoped_worker_pids(board, TASK, RUN)]
    assert 100 in pids
    assert 909 not in pids, "run 12's worker is the LIVE attempt and must never be selected by run 11's reap"


def test_t7b_run_scoped_selection_ignores_current_run_id(board):
    stored = board.execute("SELECT current_run_id FROM tasks WHERE id=?", (TASK,)).fetchone()[0]
    assert stored == NEXT_RUN
    assert 909 not in [pid for pid, _ in dispatch.run_scoped_worker_pids(board, TASK, RUN)]


def test_t8_absent_worker_is_cleared_without_a_signal(board):
    _state["alive"] = set()
    dispatch._reap_terminal_worker_row(board, _row(board), HOST, None, [])
    assert _state["terminations"] == []
    assert board.execute("SELECT worker_pid FROM task_runs WHERE id=?", (RUN,)).fetchone()[0] is None


def test_t8b_live_worker_is_terminated_and_recorded(board):
    _state["alive"] = {100}
    reaped = []
    dispatch._reap_terminal_worker_row(board, _row(board), HOST, None, reaped)
    assert _state["terminations"] == [100]
    assert reaped == [TASK]
    assert _events(board, "terminal_worker_reaped")[0]["pid"] == 100
    assert board.execute("SELECT worker_pid FROM task_runs WHERE id=?", (RUN,)).fetchone()[0] is None


def test_t8c_recycled_pid_records_both_fingerprints_and_never_signals(board):
    _state["alive"] = {100}
    _state["recycled"] = {100}
    dispatch._reap_terminal_worker_row(board, _row(board), HOST, None, [])
    assert _state["terminations"] == [], "a recycled pid belongs to someone else - it is never signalled"
    records = _events(board, "PID_REUSED")
    assert records and records[0]["recorded"] == "fp-1" and records[0]["current"] == "fp-current"
    assert board.execute("SELECT worker_pid FROM task_runs WHERE id=?", (RUN,)).fetchone()[0] == 100


def test_t8d_unresolved_outcome_prohibits_the_clear(board):
    _state["alive"] = {100}
    _state["terminates"] = False
    dispatch._reap_terminal_worker_row(board, _row(board), HOST, None, [])
    assert board.execute("SELECT worker_pid FROM task_runs WHERE id=?", (RUN,)).fetchone()[0] == 100, (
        "an unresolved termination must keep the evidence; clearing it is the confidently-say-not-running defect"
    )
    assert _events(board, "terminal_worker_reaped") == []


def test_t8e_unverified_identity_holds_the_row(board):
    board.execute("UPDATE task_runs SET worker_started_at=? WHERE id=?", (dispatch.UNVERIFIED_WORKER_FINGERPRINT, RUN))
    board.commit()
    _state["alive"] = {100}
    dispatch._reap_terminal_worker_row(board, _row(board), HOST, None, [])
    assert _state["terminations"] == []
    assert board.execute("SELECT worker_pid FROM task_runs WHERE id=?", (RUN,)).fetchone()[0] == 100


def test_t8f_foreign_host_row_is_untouched(board):
    board.execute("UPDATE task_runs SET claim_lock=? WHERE id=?", ("OTHER:1", RUN))
    board.commit()
    _state["alive"] = {100}
    dispatch._reap_terminal_worker_row(board, _row(board), HOST, None, [])
    assert _state["terminations"] == []
    assert board.execute("SELECT worker_pid FROM task_runs WHERE id=?", (RUN,)).fetchone()[0] == 100


def _add_bound(conn, pid, lock, run_id=RUN):
    """A binding WITH a spawn fingerprint, as a real bind records one.

    A binding without one is treated as UNVERIFIED (never signalled, evidence held) - correct, but it would
    make these tests pass for the wrong reason.
    """
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
        (TASK, run_id, "worker_bound",
         json.dumps({"pid": pid, "claim_lock": lock, "started_at": f"fp-{pid}"}), 0),
    )
    conn.commit()


def test_t11_all_historical_bindings_of_one_kind_are_selected(board):
    """REVIEWER-3 blocker: a run that rebound twice has TWO worker_bound events; both pids are real."""
    _add_bound(board, 201, HOST + "1b")
    _add_bound(board, 202, HOST + "1c")
    pids = [pid for pid, _ in dispatch.run_scoped_worker_pids(board, TASK, RUN)]
    assert 201 in pids and 202 in pids, f"earlier bindings of the same kind must not be dropped, got {pids}"
    assert pids.count(201) == 1 and pids.count(202) == 1, "deduplicate by pid"
    assert 909 not in pids


def test_t11b_reap_terminates_every_historical_binding(board):
    _add_bound(board, 201, HOST + "1b")
    _add_bound(board, 202, HOST + "1c")
    _state["alive"] = {100, 201, 202}
    reaped = []
    dispatch._reap_terminal_worker_row(board, _row(board), HOST, None, reaped)
    assert sorted(_state["terminations"]) == [100, 201, 202]
    assert reaped == [TASK]
    assert board.execute("SELECT worker_pid FROM task_runs WHERE id=?", (RUN,)).fetchone()[0] is None


def test_t11c_one_unresolved_historical_binding_preserves_the_evidence(board):
    """The omission the reviewer found: pid 202 stays alive, so the run row must keep its pid."""
    _add_bound(board, 201, HOST + "1b")
    _add_bound(board, 202, HOST + "1c")
    _state["alive"] = {100, 201, 202}
    real_terminate = dispatch._terminate_reclaimed_worker
    def partial(pid, lock, signal_fn=None, started_at=None):
        _state["terminations"].append(pid)
        return {"prev_pid": pid, "terminated": pid != 202, "termination_attempted": True,
                "sigkill": False, "host_local": True}
    dispatch._terminate_reclaimed_worker = partial
    try:
        dispatch._reap_terminal_worker_row(board, _row(board), HOST, None, [])
    finally:
        dispatch._terminate_reclaimed_worker = real_terminate
    assert 202 in _state["terminations"], "every selected pid is signalled"
    assert board.execute("SELECT worker_pid FROM task_runs WHERE id=?", (RUN,)).fetchone()[0] == 100, (
        "an omitted or unresolved worker must keep the run evidence; clearing here is the blocker"
    )
