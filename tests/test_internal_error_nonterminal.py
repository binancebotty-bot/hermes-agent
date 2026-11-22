"""INTERNAL_ERROR is a transient fault, not a terminal delivery outcome.

A fault that escapes the enabled path is recorded as an outcome row. That row
must not suppress the turn forever: the retry decision is made by the existing
reconciliation path (``_resume_event``), which looks at write-ahead attempt rows,
not at the outcome label.

Two distinct branches are proven separately:
- pre-send fault (no ATTEMPTED/RESENT row): no send can have happened, so the
  same turn is retry-eligible and delivers on the next call;
- post-send fault (attempt row present, outcome unknown): the send is ambiguous,
  so the next call must reconcile against the pane and never blind-resend.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = Path(os.environ.get("WAKE_PLUGIN_DIR", str(ROOT / "plugins" / "wake-emitter")))

LANE = "gpt6:assistant"
CONV = "https://chatgpt.com/c/6b01a297-292c-83ed-9f47-f49ab937c2f1"


def _load():
    sys.path.insert(0, str(PLUGIN_DIR.parent))
    return importlib.import_module("wake-emitter.emitter")


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)


def _worker(tmp_path, emitter, *, peer_map=None, logger=None):
    journal = tmp_path / "journal.jsonl"
    peer = tmp_path / "peer_lane_map.json"
    peer.write_text(json.dumps({"lanes": {LANE: {"conv": CONV, "generation": 1}}}
                               if peer_map is None else peer_map), encoding="utf-8")
    sender = tmp_path / "sender.py"
    sender.write_text("print('GUARD_COMPLETE key=x')\n", encoding="utf-8")
    return emitter.WakeEmitter(
        journal_path=str(journal), peer_map_path=str(peer), sender_path=str(sender),
        lane=LANE, enabled=True, logger=logger,
    ), journal


def _payload(**overrides):
    payload = dict(
        session_id="sess-1", task_id="task-1", turn_id="turn-1",
        completed=True, failed=False, interrupted=False,
        turn_exit_reason="text_response(stop)", model="m", platform="subagent",
    )
    payload.update(overrides)
    return payload


def _rows(journal):
    if not journal.exists():
        return []
    return [json.loads(line) for line in
            journal.read_text(encoding="utf-8").splitlines() if line.strip()]


def _outcomes(journal):
    return [row["outcome"] for row in _rows(journal) if row.get("kind") == "outcome"]


class _BadStr(Exception):
    """An exception whose own __str__ raises."""

    def __str__(self):
        raise RuntimeError("str boom")


def test_pre_send_internal_fault_is_retry_eligible_and_delivers(tmp_path):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter, peer_map=[])
    assert worker.on_session_end(**_payload()) == "INTERNAL_ERROR:AttributeError"
    rows = _rows(journal)
    assert [row["kind"] for row in rows] == ["event", "outcome"]
    assert not any(row.get("kind") == "attempt" for row in rows), \
        "the fault happened before any send intent existed"
    event_id = rows[0]["event_id"]
    rows_after_fault = rows

    # Repair the peer map and replay the SAME turn: it must deliver, not dedupe.
    worker.peer_map_path.write_text(
        json.dumps({"lanes": {LANE: {"conv": CONV, "generation": 1}}}), encoding="utf-8")
    assert worker.on_session_end(**_payload()) == "DELIVERED_UNACKED"
    assert _outcomes(journal)[-1] == "DELIVERED_UNACKED"
    assert worker._has_delivery_outcome(rows_after_fault, event_id) is False, \
        "INTERNAL_ERROR must not be a terminal delivery outcome"


def test_str_raising_exception_with_unwritable_journal_never_raises(tmp_path, monkeypatch):
    emitter = _load()
    worker, _journal = _worker(tmp_path, emitter)

    def boom(event_id, payload):
        raise _BadStr("x")

    def unwritable(event_id, outcome):
        raise OSError("journal unwritable")

    monkeypatch.setattr(worker, "_deliver_and_record", boom)
    monkeypatch.setattr(worker, "_record", unwritable)
    result = worker.on_session_end(**_payload())
    assert isinstance(result, str), "the hook must return a status string, not raise"
    assert result == "INTERNAL_ERROR:_BadStr"


def test_internal_error_counts_as_failed_not_delivered(tmp_path):
    emitter = _load()
    worker, _journal = _worker(tmp_path, emitter, peer_map=[])
    assert worker.on_session_end(**_payload()) == "INTERNAL_ERROR:AttributeError"
    status = json.loads(worker.status_path.read_text(encoding="utf-8"))
    assert status["total_events"] == 1
    assert status["total_acked"] == 0
    assert status["total_failed"] == 1, "INTERNAL_ERROR must count as failed"
    assert status["last_outcome"] == "INTERNAL_ERROR:AttributeError"


def test_journal_fault_before_event_row_is_retryable(tmp_path, monkeypatch):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)

    def unwritable(row):
        raise OSError("journal unwritable")

    monkeypatch.setattr(worker, "_append", unwritable)
    assert worker.on_session_end(**_payload()) == "INTERNAL_ERROR:OSError"
    assert _rows(journal) == [], "no event row exists, so nothing can suppress a retry"
    monkeypatch.undo()
    assert worker.on_session_end(**_payload()) == "DELIVERED_UNACKED"


def _post_send_fault_worker(tmp_path, monkeypatch):
    """One physical send, then the outcome record fails once; the INTERNAL_ERROR
    record succeeds, leaving an attempt row with no terminal delivery outcome."""
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    sends = []
    real_run = worker._run_sender

    def counting_run(argv):
        sends.append(argv)
        return real_run(argv)

    real_record = worker._record
    record_calls = []

    def flaky_record(event_id, outcome):
        record_calls.append(outcome)
        if len(record_calls) == 1:
            raise OSError("outcome record failed")
        return real_record(event_id, outcome)

    monkeypatch.setattr(worker, "_run_sender", counting_run)
    monkeypatch.setattr(worker, "_record", flaky_record)
    assert worker.on_session_end(**_payload()) == "INTERNAL_ERROR:OSError"
    assert len(sends) == 1
    rows = _rows(journal)
    assert [row["kind"] for row in rows] == ["event", "attempt", "outcome"]
    assert rows[-1]["outcome"] == "INTERNAL_ERROR:OSError"
    return worker, journal, real_record, sends, rows[0]["event_id"]


def test_post_send_internal_fault_with_present_body_does_not_resend(tmp_path, monkeypatch):
    worker, _journal, real_record, sends, event_id = _post_send_fault_worker(tmp_path, monkeypatch)
    monkeypatch.setattr(worker, "_record", real_record)
    monkeypatch.setattr(worker, "_read_pane_turns",
                        lambda lane, timeout: [f"EVENT-ID: {event_id}"])
    assert worker.on_session_end(**_payload()) == "DELIVERED_UNCONFIRMED"
    assert len(sends) == 1, "a post-send fault must never produce a second physical send"


def test_post_send_internal_fault_unreadable_pane_fails_closed(tmp_path, monkeypatch):
    worker, _journal, real_record, sends, _event_id = _post_send_fault_worker(tmp_path, monkeypatch)
    monkeypatch.setattr(worker, "_record", real_record)

    def unreadable(lane, timeout):
        raise RuntimeError("pane unreadable")

    monkeypatch.setattr(worker, "_read_pane_turns", unreadable)
    assert worker.on_session_end(**_payload()) == "RECONCILE_UNAVAILABLE"
    assert len(sends) == 1, "an unreadable pane must fail closed without a resend"
