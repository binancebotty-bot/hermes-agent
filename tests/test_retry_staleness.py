"""Retry staleness horizon: an old failed wake settles instead of arriving hours late.

The live journal showed REFUSED: events from two hours earlier still retry-eligible.
A non-terminal outcome inside retry_horizon_seconds stays retryable; past it the
event settles ABANDONED_STALE once and can never reach the sender.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from test_internal_error_nonterminal import _load, _payload, _rows, _worker

STALE_EVENT_ID = "203f9762"
STALE_AGE = 7200  # two hours, matching the live journal
FRESH_AGE = 60


def _utc_ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def _seed(journal, event_id, outcome, age, *, turn_id, with_attempt=False):
    """Seed a prior invocation's durable rows in the real journal."""
    stamp = _utc_ago(age)
    rows = [{
        "kind": "event", "event_id": event_id, "dedupe_key": f"turn:{turn_id}",
        "utc": stamp, "task_id": "cron:old:1", "turn_id": turn_id,
    }]
    if with_attempt:
        rows.append({"kind": "attempt", "event_id": event_id, "utc": stamp,
                     "outcome": "ATTEMPTED", "argv": []})
    rows.append({"kind": "outcome", "event_id": event_id, "utc": stamp,
                 "outcome": outcome})
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _counting_sender(worker, monkeypatch):
    """Count physical sends while still running the stub sender."""
    sends = []
    real_run = worker._run_sender

    def counting(argv):
        sends.append(argv)
        return real_run(argv)

    monkeypatch.setattr(worker, "_run_sender", counting)
    return sends


def _outcomes(journal):
    return [r["outcome"] for r in _rows(journal) if r.get("kind") == "outcome"]


def test_a_stale_refused_event_settles_abandoned_stale_with_zero_sends(tmp_path, monkeypatch):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    sends = _counting_sender(worker, monkeypatch)
    _seed(journal, STALE_EVENT_ID, "REFUSED:EXIT_2", STALE_AGE, turn_id="turn-old")

    result = worker.on_session_end(**_payload(turn_id="turn-old"))

    assert result == "ABANDONED_STALE"
    assert sends == [], "a stale event must never reach the sender"
    assert _outcomes(journal)[-1] == "ABANDONED_STALE"


def test_b_refused_event_inside_horizon_still_retries_and_delivers(tmp_path, monkeypatch):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    sends = _counting_sender(worker, monkeypatch)
    _seed(journal, "fresh-refused", "REFUSED:EXIT_2", FRESH_AGE, turn_id="turn-fresh")

    result = worker.on_session_end(**_payload(turn_id="turn-fresh"))

    assert result == "DELIVERED_UNACKED"
    assert len(sends) == 1, "an in-horizon event must still be delivered"
    assert _outcomes(journal)[-1] == "DELIVERED_UNACKED"


def test_c_stale_event_settles_once_with_no_second_row(tmp_path, monkeypatch):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    sends = _counting_sender(worker, monkeypatch)
    _seed(journal, STALE_EVENT_ID, "REFUSED:EXIT_1", STALE_AGE, turn_id="turn-old")

    assert worker.on_session_end(**_payload(turn_id="turn-old")) == "ABANDONED_STALE"
    before = _rows(journal)
    assert sum(r.get("outcome") == "ABANDONED_STALE" for r in before) == 1

    worker.on_session_end(**_payload(turn_id="turn-old"))

    assert _rows(journal) == before, "a second invocation must add no row"
    assert sends == []


def test_d_stale_event_does_not_block_fresh_event_on_same_lane(tmp_path, monkeypatch):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    sends = _counting_sender(worker, monkeypatch)
    _seed(journal, STALE_EVENT_ID, "REFUSED:EXIT_1", STALE_AGE, turn_id="turn-old")

    result = worker.on_session_end(**_payload(turn_id="turn-new"))

    fresh = [r for r in _rows(journal)
             if r.get("kind") == "event" and r.get("event_id") != STALE_EVENT_ID]
    assert len(fresh) == 1
    fresh_id = fresh[0]["event_id"]
    assert result == "DELIVERED_UNACKED"
    assert len(sends) == 1
    assert any(fresh_id in arg for arg in sends[0]), "the send must carry the fresh event id"
    assert not any(STALE_EVENT_ID in arg for arg in sends[0]), "no stale id may be sent"
    assert _outcomes(journal)[-1] == "DELIVERED_UNACKED"


def test_e_delivery_error_inside_horizon_still_delivers_on_retry(tmp_path, monkeypatch):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    sends = _counting_sender(worker, monkeypatch)
    _seed(journal, "err-event", "DELIVERY_ERROR:FileNotFoundError", FRESH_AGE,
          turn_id="turn-err", with_attempt=True)
    monkeypatch.setattr(worker, "_read_pane_turns",
                        lambda lane, timeout: ["EVENT-ID: err-event"])

    result = worker.on_session_end(**_payload(turn_id="turn-err"))

    assert result == "DELIVERED_UNCONFIRMED"
    assert sends == [], "the attempt row already exists: settle from the pane, never resend"
    assert _outcomes(journal)[-1] == "DELIVERED_UNCONFIRMED"


def test_f_reconcile_unavailable_inside_horizon_still_delivers_on_retry(tmp_path, monkeypatch):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    sends = _counting_sender(worker, monkeypatch)
    _seed(journal, "unavail-event", "RECONCILE_UNAVAILABLE", FRESH_AGE,
          turn_id="turn-unavail", with_attempt=True)
    monkeypatch.setattr(worker, "_read_pane_turns",
                        lambda lane, timeout: ["EVENT-ID: unavail-event"])

    result = worker.on_session_end(**_payload(turn_id="turn-unavail"))

    assert result == "DELIVERED_UNCONFIRMED"
    assert sends == [], "a readable pane settles the event, no resend"
    assert _outcomes(journal)[-1] == "DELIVERED_UNCONFIRMED"
