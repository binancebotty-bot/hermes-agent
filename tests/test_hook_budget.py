"""FIX 2 + FIX 3: transient delivery failures and the bounded hook budget.

(e)-(f) RECONCILE_UNAVAILABLE and DELIVERY_ERROR are non-terminal: the event
stays retry-eligible through the attempt-row reconciliation, and a later
readable pane / repaired sender settles it with no second physical send.
(g) a send that cannot finish inside hook_budget_seconds records
DELIVERY_PENDING and the hook returns inside the budget; the event stays open
and the next invocation reconciles instead of resending.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = Path(os.environ.get("WAKE_PLUGIN_DIR", str(ROOT / "plugins" / "wake-emitter")))

LANE = "gpt6:assistant"
CONV = "https://chatgpt.com/c/6b01a297-292c-83ed-9f47-f49ab937c2f1"


def _load():
    sys.path.insert(0, str(PLUGIN_DIR.parent))
    return importlib.import_module("wake-emitter.emitter")


def _worker(tmp_path, emitter, *, hook_budget_seconds=None, sender_text=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    journal = tmp_path / "journal.jsonl"
    peer = tmp_path / "peer_lane_map.json"
    peer.write_text(json.dumps({"lanes": {LANE: {"conv": CONV, "generation": 1}}}),
                    encoding="utf-8")
    sender = tmp_path / "sender.py"
    sender.write_text(sender_text or "print('GUARD_COMPLETE key=x')\n", encoding="utf-8")
    kwargs = dict(journal_path=str(journal), peer_map_path=str(peer), sender_path=str(sender),
                  lane=LANE, enabled=True)
    if hook_budget_seconds is not None:
        kwargs["hook_budget_seconds"] = hook_budget_seconds
    return emitter.WakeEmitter(**kwargs), journal


def _payload(**overrides):
    payload = dict(session_id="sess-1", task_id="cron:abc:1", turn_id="turn-1",
                   completed=True, failed=False, interrupted=False,
                   turn_exit_reason="text_response(stop)", model="m", platform="cron")
    payload.update(overrides)
    return payload


def _rows(journal):
    if not journal.exists():
        return []
    return [json.loads(line) for line in
            journal.read_text(encoding="utf-8").splitlines() if line.strip()]


def _counting_sender(worker, monkeypatch):
    """Count physical sends while still running the real sender."""
    sends = []
    real_run = worker._run_sender

    def counting(argv):
        sends.append(argv)
        return real_run(argv)

    monkeypatch.setattr(worker, "_run_sender", counting)
    return sends


def test_e_reconcile_unavailable_then_readable_pane_delivers_one_send(tmp_path, monkeypatch):
    """(e) A transient pane fault leaves the event open; a later readable pane
    settles it as DELIVERED_UNCONFIRMED with no second physical send.
    """
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    calls = _counting_sender(worker, monkeypatch)

    # First send lands, then its outcome record faults: an attempt row with no
    # terminal delivery outcome (the review's open-event state).
    real_record = worker._record
    state = {"n": 0}

    def flaky_record(event_id, outcome, details=None):
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("outcome record failed")
        return real_record(event_id, outcome, details)

    monkeypatch.setattr(worker, "_record", flaky_record)
    assert worker.on_session_end(**_payload()) == "INTERNAL_ERROR:OSError"
    assert len(calls) == 1
    event_id = _rows(journal)[0]["event_id"]
    monkeypatch.setattr(worker, "_record", real_record)

    # Transient pane fault: still open, still no resend.
    def unreadable(lane, timeout):
        raise RuntimeError("pane unreadable")

    monkeypatch.setattr(worker, "_read_pane_turns", unreadable)
    assert worker.on_session_end(**_payload()) == "RECONCILE_UNAVAILABLE"
    assert len(calls) == 1, "an unreadable pane must not resend"

    # Pane recovers and shows the body: the event settles, still one send.
    monkeypatch.setattr(worker, "_read_pane_turns",
                        lambda lane, timeout: [f"EVENT-ID: {event_id}"])
    assert worker.on_session_end(**_payload()) == "DELIVERED_UNCONFIRMED"
    assert len(calls) == 1, "exactly one physical send across reconcile and delivery"


def test_f_delivery_error_then_working_sender_delivers_one_send(tmp_path, monkeypatch):
    """(f) A sender fault is non-terminal; with the sender repaired the event
    settles from the pane and the sender is never invoked a second time.
    """
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    calls = []
    real_run = worker._run_sender
    state = {"n": 0}

    def flaky_sender(argv):
        calls.append(argv)
        state["n"] += 1
        if state["n"] == 1:
            raise FileNotFoundError("sender interpreter missing")
        return real_run(argv)

    monkeypatch.setattr(worker, "_run_sender", flaky_sender)
    assert worker.on_session_end(**_payload()) == "DELIVERY_ERROR:FileNotFoundError"
    assert len(calls) == 1
    assert any(row.get("kind") == "attempt" for row in _rows(journal)), \
        "the write-ahead intent must exist, so a resend is impossible"
    event_id = _rows(journal)[0]["event_id"]

    # The sender is repaired and the pane carries the body the send submitted.
    monkeypatch.setattr(worker, "_read_pane_turns",
                        lambda lane, timeout: [f"EVENT-ID: {event_id}"])
    assert worker.on_session_end(**_payload()) == "DELIVERED_UNCONFIRMED"
    assert len(calls) == 1, "a repaired sender must not produce a second physical send"


def test_g_slow_sender_records_pending_and_returns_inside_budget(tmp_path, monkeypatch):
    """(g) A send that cannot finish inside hook_budget_seconds records
    DELIVERY_PENDING, the hook returns inside the budget, and the next
    invocation reconciles without a second physical send.
    """
    emitter = _load()
    slow = "import time\nprint('GUARD_COMPLETE key=x')\ntime.sleep(30)\n"
    worker, journal = _worker(tmp_path, emitter, hook_budget_seconds=1.0, sender_text=slow)
    sends = _counting_sender(worker, monkeypatch)

    start = time.monotonic()
    result = worker.on_session_end(**_payload())
    elapsed = time.monotonic() - start

    assert result == "DELIVERY_PENDING"
    assert elapsed < 1.0, f"the hook must return inside its budget, took {elapsed:.2f}s"
    rows = _rows(journal)
    event_id = rows[0]["event_id"]
    assert not worker._has_delivery_outcome(rows, event_id), "the event must stay open"
    assert any(row.get("kind") == "attempt" for row in rows), \
        "the write-ahead intent must exist, so a resend is impossible"

    monkeypatch.setattr(worker, "_read_pane_turns",
                        lambda lane, timeout: ["no body yet"])
    assert worker.on_session_end(**_payload()) == "RECONCILE_UNPROVEN"
    assert len(sends) == 1, "the next invocation must reconcile, never resend"


def test_hook_budget_defaults_to_20_and_is_configurable(tmp_path):
    """hook_budget_seconds defaults to 20 s and is read from plugin config."""
    emitter = _load()
    default_worker, _ = _worker(tmp_path / "default", emitter)
    assert default_worker.hook_budget_seconds == 20.0
    tuned_worker, _ = _worker(tmp_path / "tuned", emitter, hook_budget_seconds=7.5)
    assert tuned_worker.hook_budget_seconds == 7.5
