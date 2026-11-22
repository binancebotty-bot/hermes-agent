"""W1j: ACK settlement hardening. Staging only; stub transports, synthesised rows.

Five review findings, each with a test that FAILS on the pre-fix emitter:
  1 our own two-line outbound envelope must never settle (self-ACK);
  2 expiry must never eat a genuine late ACK (settle before expire);
  3 the (outcome, ack_binding) pair is enforced at the journal write itself;
  4 status counts reconcile against the journal and ack_label is the newest event;
  5 age is guarded against a forwards or backwards wall-clock step.
Nothing here installs, sends, configures, restarts or contacts an agent.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from test_binding import CONV_B, GEN, _bind, _load, _outcomes, _payload, _rows, _worker
from test_w1h_ack_settlement import (
    KEYWORD_MISSING,
    SETTLED,
    UNPROVEN_EXPIRED,
    PAIRING_INVALID,
    _deliver,
    _outcome_names,
    _sweep,
    _untimed,
)


def _two_line_envelope(event_id):
    """The REAL payload _sender_argv writes to the pane: two lines, one turn."""
    return (
        "RC-w1j-%s | EVENT-ID: %s WAKE from Hermes (hermes:mcp-integration)\n"
        "Reply with this exact line and nothing else: WAKE-ACK %s"
        % (event_id, event_id, event_id)
    )


def _delivery(event_id):
    return _untimed("EVENT-ID: " + event_id)


def _peer_ack(event_id):
    return _untimed("WAKE-ACK " + event_id)


# --- finding 1: our own outbound envelope is not an ACK --------------------

def test_1a_real_two_line_envelope_is_not_an_ack_candidate():
    """Unit match: the exact two-line payload the sender writes is rejected."""
    emitter = _load()
    eid = "11111111-1111-4111-8111-111111111111"
    envelope = _two_line_envelope(eid)
    assert emitter._ack_reply_half(envelope) == (
        "Reply with this exact line and nothing else: WAKE-ACK " + eid)
    assert emitter._ack_candidate(envelope, eid) is False, \
        "our own two-line envelope must not match as an ACK"


def test_1b_real_two_line_envelope_never_settles(tmp_path, monkeypatch):
    """Sweep: the real envelope in the pane cannot settle its own event."""
    worker, journal, eid, sends = _deliver(tmp_path, monkeypatch, "w1j-self-ack")
    _sweep(worker, monkeypatch, [_untimed(_two_line_envelope(eid))])
    names = _outcome_names(journal, eid)
    assert not any(name in SETTLED for name in names), \
        "our own outbound envelope must never settle: %s" % names
    assert len(sends) == 1, "a refused self-ACK must never resend"


def test_1c_genuine_peer_wake_ack_still_settles(tmp_path, monkeypatch):
    """Guard: a real peer WAKE-ACK after the delivery anchor still settles."""
    worker, journal, eid, sends = _deliver(tmp_path, monkeypatch, "w1j-peer-ack")
    _sweep(worker, monkeypatch, [_delivery(eid), _peer_ack(eid)])
    names = _outcome_names(journal, eid)
    assert any(name in SETTLED for name in names), \
        "a genuine peer WAKE-ACK must settle: %s" % names
    assert len(sends) == 1, "settlement must never resend"


# --- finding 2: expiry follows the pane read, never precedes it ------------

def test_2a_late_ack_beats_sweep_count_expiry(tmp_path, monkeypatch):
    """A genuine ACK on the sweep that would expire must settle, not expire."""
    worker, journal, eid, sends = _deliver(tmp_path, monkeypatch, "w1j-late-sweep")
    worker.ack_unproven_max_sweeps = 1
    worker.ack_unproven_max_seconds = 1.0e9
    _sweep(worker, monkeypatch, [_peer_ack(eid)])  # unproven: no delivery anchor
    assert "ACK_DELIVERY_UNPROVEN" in _outcome_names(journal, eid)

    _sweep(worker, monkeypatch, [_delivery(eid), _peer_ack(eid)])
    names = _outcome_names(journal, eid)
    assert any(name in SETTLED for name in names), names
    assert UNPROVEN_EXPIRED not in names, "expiry ate a genuine late ACK: %s" % names
    assert len(sends) == 1


def test_2b_late_ack_beats_elapsed_expiry(tmp_path, monkeypatch):
    """An aged unproven row must not expire when a genuine ACK is in the pane."""
    worker, journal, eid, sends = _deliver(tmp_path, monkeypatch, "w1j-late-elapsed")
    worker.ack_unproven_max_sweeps = 99
    worker.ack_unproven_max_seconds = 1.0
    _sweep(worker, monkeypatch, [_peer_ack(eid)])
    assert "ACK_DELIVERY_UNPROVEN" in _outcome_names(journal, eid)

    stale = "2020-01-01T00:00:00Z"
    rows = _rows(journal)
    for row in rows:
        if row.get("event_id") == eid and row.get("utc") and row.get("kind") in (
                "event", "outcome", "attempt"):
            row["utc"] = stale
    journal.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    _sweep(worker, monkeypatch, [_delivery(eid), _peer_ack(eid)])
    names = _outcome_names(journal, eid)
    assert any(name in SETTLED for name in names), names
    assert UNPROVEN_EXPIRED not in names, names
    assert len(sends) == 1


# --- finding 3: pairing enforced at the journal write ----------------------

def test_3a_record_rejects_invalid_pair_without_writing(tmp_path, monkeypatch):
    """_record itself refuses a mismatched ACK pair and writes no row."""
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    before = len(_rows(journal))
    result = worker._record("pair-event", "DELIVERED_ACKED",
                            {"ack_binding": "order_only"})
    assert result == PAIRING_INVALID, \
        "_record must return the named pairing error: %r" % (result,)
    assert len(_rows(journal)) == before, "a rejected pair must write no row"


def test_3b_record_assistant_ack_delegates_to_record(tmp_path, monkeypatch):
    """The public seam funnels through the guarded _record, not _append."""
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    worker._write_row({"kind": "event", "event_id": "delegated-event",
                       "utc": emitter._utc_now()})
    calls = []
    real_record = worker._record

    def spy(event_id, outcome, details=None):
        calls.append((event_id, outcome, dict(details or {})))
        return real_record(event_id, outcome, details)

    monkeypatch.setattr(worker, "_record", spy)
    assert worker.record_assistant_ack("delegated-event") is True
    assert len(calls) == 1, "record_assistant_ack must delegate to _record: %s" % calls
    assert calls[0][1] == "DELIVERED_ACKED"
    assert calls[0][2].get("ack_binding") == "time_verified"


# --- finding 4: status reconciles against the journal ----------------------

def test_4_status_counts_reconcile_and_ack_label_is_newest_event(tmp_path, monkeypatch):
    """Counts sum to total_events; ack_label follows the newest event only."""
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    rows = [
        {"kind": "event", "event_id": "A", "utc": "2026-01-01T00:00:00Z"},
        {"kind": "outcome", "event_id": "A", "utc": "2026-01-01T00:01:00Z",
         "outcome": "DELIVERED_ACKED", "ack_binding": "time_verified"},
        {"kind": "event", "event_id": "B", "utc": "2026-01-01T00:02:00Z"},
        {"kind": "event", "event_id": "B", "utc": "2026-01-01T00:03:00Z"},
    ]
    journal.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    worker._write_status()
    record = json.loads(worker.status_path.read_text(encoding="utf-8"))
    counts = record.get("outcome_counts", {})
    assert sum(counts.values()) == record.get("total_events") == 3, \
        "outcome_counts must reconcile with total_events: %s" % record
    assert record.get("ack_label") is None, \
        "ack_label must describe the newest event (B, no outcome), not an old ACK: %s" % record


# --- finding 5: clock steps cannot silently expire or resurrect ------------

def test_5a_forward_clock_step_does_not_age(tmp_path, monkeypatch):
    """A forwards wall-clock jump must not inflate age (CLOCK_STEP_GUARD)."""
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    row = {"utc": emitter._utc_now()}
    real_now = datetime.now(timezone.utc)

    class Jumped(emitter.datetime):
        @classmethod
        def now(cls, tz=None):
            return real_now + timedelta(hours=1)

    monkeypatch.setattr(emitter, "datetime", Jumped)
    age = worker._age_seconds(row)
    assert age is not None and abs(age) <= worker.ack_skew_seconds + 1.0, \
        "a forwards clock step must not age an event by an hour: %r" % age


def test_5b_backward_clock_step_does_not_resurrect(tmp_path, monkeypatch):
    """A backwards wall-clock jump must not make age negative (CLOCK_STEP_GUARD)."""
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    row = {"utc": emitter._utc_now()}
    real_now = datetime.now(timezone.utc)

    class Back(emitter.datetime):
        @classmethod
        def now(cls, tz=None):
            return real_now - timedelta(hours=1)

    monkeypatch.setattr(emitter, "datetime", Back)
    age = worker._age_seconds(row)
    assert age is not None and age >= 0.0 and abs(age) <= worker.ack_skew_seconds + 1.0, \
        "a backwards clock step must not resurrect an event: %r" % age
