"""W1d: close the three fail-open ACK holes declared by the W1c author.

Only stub transports and synthesised pane records are used. No live plugin tree,
config, gateway, cron or conversation is touched.

(a) Peer map has no entry for the delivered conversation -> ACK_GENERATION_UNKNOWN,
    no settlement, retryable; settles once the entry appears.
(b) ACK turn timestamp predating the delivery utc is refused.
(c) ACK turn timestamp at/after delivery (inside the named skew tolerance) settles.
(d) ACK turn with no timestamp is UNPROVEN and does not settle.
(e) Readback window omitting the delivery turn does not settle; one bounded wider
    re-read is attempted, then the outcome is UNPROVEN.
(f) The clock-skew tolerance is a named config value, not a hidden constant.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from test_binding import (
    CONV_B, LANE_A, LANE_B, GEN, _bind, _load, _outcomes, _payload, _rows, _worker,
)


def _shift(iso, seconds):
    """Offset an ISO-8601 UTC stamp by whole seconds, keeping the Z form."""
    stamp = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return ((stamp + timedelta(seconds=seconds)).isoformat(timespec="seconds")
            .replace("+00:00", "Z"))


def _turn(text, utc=None):
    record = {"text": text}
    if utc is not None:
        record["utc"] = utc
    return record


def _delivery_turn(event_id, utc):
    return _turn("EVENT-ID: %s" % event_id, utc)


def _ack_turn(event_id, utc=None):
    return _turn("WAKE-ACK %s" % event_id, utc)


def _deliver(tmp_path, monkeypatch, task_id):
    """One real on_session_end delivery with a stub sender; return its journal."""
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    _bind(worker, task_id, conv=CONV_B, generation=GEN)
    sends = []

    def stub_sender(argv):
        sends.append(argv)
        return "DELIVERED_UNACKED"

    monkeypatch.setattr(worker, "_run_sender", stub_sender)
    assert worker.on_session_end(
        **_payload(task_id, turn_id="turn-" + task_id)) == "DELIVERED_UNACKED"
    event = [r for r in _rows(journal) if r.get("kind") == "event"][-1]
    return worker, journal, event["event_id"], sends


def _delivery_utc(journal, event_id):
    attempt = [r for r in _rows(journal) if r.get("kind") == "attempt"
               and r.get("event_id") == event_id][-1]
    return attempt["utc"]


def _sweep(worker, monkeypatch, pane):
    """Reconcile once with a stub pane that can see the bounded wider window."""
    reads = []

    def stub(lane, timeout, window=None):
        reads.append((lane, window))
        return pane(lane, window)

    monkeypatch.setattr(worker, "_read_pane_turns", stub)
    worker.on_session_end(**_payload("noop", turn_id=None, completed=False))
    return reads


def _no_acked(outcomes, event_id):
    return not any(r["outcome"] == "DELIVERED_ACKED" for r in outcomes)


# --- (a) missing peer-map entry must fail closed, then be retryable --------

def test_a_absent_peer_map_entry_is_unknown_not_settled_then_settles_when_present(
        tmp_path, monkeypatch):
    worker, journal, eid, sends = _deliver(tmp_path, monkeypatch, "w1d-a")
    peer = json.loads(worker.peer_map_path.read_text(encoding="utf-8"))
    peer["lanes"] = {LANE_A: peer["lanes"][LANE_A]}  # CONV_B entry removed
    worker.peer_map_path.write_text(json.dumps(peer), encoding="utf-8")

    utc = _delivery_utc(journal, eid)
    turns = [_delivery_turn(eid, utc), _ack_turn(eid, _shift(utc, 5))]
    _sweep(worker, monkeypatch, lambda lane, window: list(turns))

    row = _outcomes(journal, eid)[-1]
    assert row["outcome"] == "ACK_GENERATION_UNKNOWN", \
        "absent peer-map entry must record ACK_GENERATION_UNKNOWN, got %s" % row["outcome"]
    assert _no_acked(_outcomes(journal, eid), eid), \
        "absence from the peer map must never settle an ACK"
    assert len(sends) == 1, "no reconciliation path may resend"

    # Retryable: restore the entry and the very next sweep may settle.
    peer["lanes"][LANE_B] = {"conv": CONV_B, "generation": GEN}
    worker.peer_map_path.write_text(json.dumps(peer), encoding="utf-8")
    _sweep(worker, monkeypatch, lambda lane, window: list(turns))
    assert _outcomes(journal, eid)[-1]["outcome"] == "DELIVERED_ACKED", \
        "an event held at ACK_GENERATION_UNKNOWN must settle once the entry appears"
    assert len(sends) == 1, "settling later must still never resend"


# --- (b) ACK timestamp predating delivery is refused ----------------------

def test_b_ack_timestamp_predating_delivery_is_refused(tmp_path, monkeypatch):
    worker, journal, eid, _ = _deliver(tmp_path, monkeypatch, "w1d-b")
    utc = _delivery_utc(journal, eid)
    turns = [_delivery_turn(eid, utc), _ack_turn(eid, _shift(utc, -120))]
    _sweep(worker, monkeypatch, lambda lane, window: list(turns))

    row = _outcomes(journal, eid)[-1]
    assert row["outcome"] == "ACK_BEFORE_DELIVERY", \
        "an ACK turn predating delivery must be refused, got %s" % row["outcome"]
    assert _no_acked(_outcomes(journal, eid), eid), \
        "wall-clock order, not turn order alone, must gate settlement"


# --- (c) ACK inside the skew tolerance after delivery settles -------------

def test_c_ack_timestamp_after_delivery_settles_with_named_tolerance(
        tmp_path, monkeypatch):
    emitter = _load()
    worker, journal, eid, _ = _deliver(tmp_path, monkeypatch, "w1d-c")
    assert worker.ack_skew_seconds == emitter.ACK_SKEW_SECONDS, \
        "the clock-skew tolerance must be a named config value"
    utc = _delivery_utc(journal, eid)
    turns = [_delivery_turn(eid, utc), _ack_turn(eid, _shift(utc, 3))]
    _sweep(worker, monkeypatch, lambda lane, window: list(turns))

    assert _outcomes(journal, eid)[-1]["outcome"] == "DELIVERED_ACKED", \
        "an ACK after delivery inside skew tolerance must still settle"

    # The tolerance itself is used: a few seconds early (inside skew) settles.
    worker2, journal2, eid2, _ = _deliver(tmp_path / "early", monkeypatch, "w1d-c-early")
    utc2 = _delivery_utc(journal2, eid2)
    turns2 = [_delivery_turn(eid2, utc2), _ack_turn(eid2, _shift(utc2, -3))]
    _sweep(worker2, monkeypatch, lambda lane, window: list(turns2))
    assert _outcomes(journal2, eid2)[-1]["outcome"] == "DELIVERED_ACKED", \
        "an ACK inside the named skew tolerance must settle even a few seconds early"


# --- (d) ACK turn without a timestamp is UNPROVEN -------------------------

def test_d_ack_turn_without_timestamp_does_not_settle(tmp_path, monkeypatch):
    worker, journal, eid, _ = _deliver(tmp_path, monkeypatch, "w1d-d")
    utc = _delivery_utc(journal, eid)
    turns = [_delivery_turn(eid, utc), _ack_turn(eid)]  # no ack timestamp
    _sweep(worker, monkeypatch, lambda lane, window: list(turns))

    row = _outcomes(journal, eid)[-1]
    assert row["outcome"] == "ACK_TIME_UNPROVEN", \
        "a timestamp-less ACK turn must be UNPROVEN, got %s" % row["outcome"]
    assert _no_acked(_outcomes(journal, eid), eid), \
        "an unproven time binding must never settle"


# --- (e) truncated window: one bounded re-read, then UNPROVEN -------------

def test_e_window_omitting_delivery_turn_rereads_once_and_stays_unproven(
        tmp_path, monkeypatch):
    emitter = _load()
    worker, journal, eid, _ = _deliver(tmp_path, monkeypatch, "w1d-e")
    utc = _delivery_utc(journal, eid)
    turns = [_ack_turn(eid, _shift(utc, 5))]  # delivery turn absent from window
    reads = _sweep(worker, monkeypatch, lambda lane, window: list(turns))

    row = _outcomes(journal, eid)[-1]
    assert row["outcome"] == "ACK_DELIVERY_UNPROVEN", \
        "a truncated window must be UNPROVEN, got %s" % row["outcome"]
    assert _no_acked(_outcomes(journal, eid), eid), \
        "a nonce alone in a truncated window must never settle"
    assert reads == [(LANE_B, None), (LANE_B, emitter.ACK_REREAD_WINDOW)], \
        "exactly one bounded wider re-read is allowed: %s" % reads

    # The wider read's content is consumed, and the UNPROVEN outcome is retryable.
    _sweep(worker, monkeypatch,
           lambda lane, window: ([_delivery_turn(eid, utc)] + list(turns))
           if window is not None else list(turns))
    assert _outcomes(journal, eid)[-1]["outcome"] == "DELIVERED_ACKED", \
        "a wider read containing the delivery turn must settle the retry"


# --- (f) the tolerance is configurable, not a literal buried in code -----

def test_f_skew_tolerance_is_read_from_named_config(tmp_path):
    emitter = _load()

    class Ctx:
        logger = None

        def get_config(self, key, default=None):
            return {"ack_skew_seconds": 7}.get(key, default)

    worker = emitter.build_from_ctx(Ctx())
    assert worker.ack_skew_seconds == 7, \
        "ack_skew_seconds must be a named, operator-settable config value"
