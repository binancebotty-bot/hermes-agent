"""W1e: capability-aware ACK binding — no silent weakening, no permanent stall.

A readback whose turns carry timestamps keeps W1d's strict wall-clock gate. A
readback with NO turn timestamps at all (the real pane_read emits every turn as
``[unknown]``) degrades to an order-only binding with a visibly distinct label:
the delivery turn must be present, the ACK must follow it in order, and the
nonce must be on the ACK. Only stub transports and synthesised pane records are
used: no live install, config, gateway, cron or conversation is touched.

(a) Timestamped ACK after delivery settles with a time-verified label.
(b) Timestamp-less readback with delivery turn + ordered ACK -> ACKED_ORDER_ONLY.
(c) Timestamp-less readback with delivery turn absent -> ACK_DELIVERY_UNPROVEN.
(d) Timestamp-less ACK before the delivery turn in order refuses.
(e) No readback call is made with an unsupported --window flag.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from test_binding import (
    CONV_B, GEN, _bind, _load, _outcomes, _payload, _rows, _worker,
)


def _shift(iso, seconds):
    stamp = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return ((stamp + timedelta(seconds=seconds)).isoformat(timespec="seconds")
            .replace("+00:00", "Z"))


def _deliver(tmp_path, monkeypatch, task_id):
    """One real on_session_end delivery with a stub sender; return its journal."""
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    _bind(worker, task_id, conv=CONV_B, generation=GEN)
    # Hermetic capability fixture: this readback has NO --window flag.
    reader = tmp_path / "pane_read.py"
    reader.write_text("# synthesised readback: bounded tail only\n", encoding="utf-8")
    worker.pane_read_path = str(reader)
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


def _sweep(worker, monkeypatch, turns):
    """One reconcile invocation; records every (lane, window) readback call."""
    reads = []

    def stub(lane, timeout, window=None):
        reads.append((lane, window))
        return list(turns)

    monkeypatch.setattr(worker, "_read_pane_turns", stub)
    worker.on_session_end(**_payload("noop", turn_id=None, completed=False))
    return reads


def _untimed(text):
    """A turn with no timestamp field, exactly as the live pane_read emits."""
    return {"text": text}


def _timed(text, utc):
    return {"text": text, "utc": utc}


# --- (a) strict timestamped path stays time-verified ----------------------

def test_a_timestamped_ack_after_delivery_is_time_verified(tmp_path, monkeypatch):
    worker, journal, eid, sends = _deliver(tmp_path, monkeypatch, "w1e-a")
    utc = _delivery_utc(journal, eid)
    turns = [_timed("EVENT-ID: %s" % eid, utc),
             _timed("WAKE-ACK %s" % eid, _shift(utc, 3))]
    _sweep(worker, monkeypatch, turns)

    row = _outcomes(journal, eid)[-1]
    assert row["outcome"] == "DELIVERED_ACKED", \
        "a timestamped ACK after delivery must keep the strict settlement: %s" % row
    assert row.get("ack_binding") == "time_verified", \
        "the time-verified path must be distinguishable from order-only: %s" % row
    assert len(sends) == 1, "settlement must never resend"


# --- (b) timestamp-less readback degrades to a labelled order-only ACK -----

def test_b_untimed_readback_with_ordered_ack_settles_order_only(tmp_path, monkeypatch):
    worker, journal, eid, sends = _deliver(tmp_path, monkeypatch, "w1e-b")
    turns = [_untimed("EVENT-ID: %s" % eid), _untimed("WAKE-ACK %s" % eid)]
    _sweep(worker, monkeypatch, turns)

    row = _outcomes(journal, eid)[-1]
    assert row["outcome"] == "ACKED_ORDER_ONLY", \
        "a timestamp-less readback must degrade to a labelled order-only settlement: %s" % row
    assert row.get("ack_binding") == "order_only", \
        "the order-only label must say time was not verified: %s" % row
    assert row.get("ack_message_ref"), "order-only must still record the matched turn ref"
    assert len(sends) == 1, "settlement must never resend"


# --- (c) no delivery anchor -> refuse even with an ordered-looking nonce ---

def test_c_untimed_readback_without_delivery_turn_refuses(tmp_path, monkeypatch):
    worker, journal, eid, sends = _deliver(tmp_path, monkeypatch, "w1e-c")
    reads = _sweep(worker, monkeypatch, [_untimed("WAKE-ACK %s" % eid)])

    row = _outcomes(journal, eid)[-1]
    assert row["outcome"] == "ACK_DELIVERY_UNPROVEN", \
        "without the delivery anchor there is no order to trust: %s" % row
    assert not any(r["outcome"] in ("DELIVERED_ACKED", "ACKED_ORDER_ONLY")
                   for r in _outcomes(journal, eid)), "must not settle"
    assert all(w is None for _, w in reads), \
        "a readback without --window must not be re-read with it: %s" % reads
    assert len(sends) == 1, "refusal must never resend"


# --- (d) ACK ahead of the delivery anchor in turn order -> refuse ----------

def test_d_untimed_ack_before_delivery_in_order_refuses(tmp_path, monkeypatch):
    worker, journal, eid, sends = _deliver(tmp_path, monkeypatch, "w1e-d")
    turns = [_untimed("WAKE-ACK %s" % eid), _untimed("EVENT-ID: %s" % eid)]
    _sweep(worker, monkeypatch, turns)

    row = _outcomes(journal, eid)[-1]
    assert row["outcome"] == "ACK_BEFORE_DELIVERY", \
        "an ACK ahead of its delivery anchor in turn order must refuse: %s" % row
    assert len(sends) == 1, "refusal must never resend"


# --- (e) never pass an unsupported --window flag ---------------------------

def test_e_no_readback_call_uses_unsupported_window_flag(tmp_path, monkeypatch):
    worker, journal, eid, sends = _deliver(tmp_path, monkeypatch, "w1e-e")
    calls = []

    def stub(lane, timeout, **kwargs):
        calls.append(kwargs)
        # Delivery turn absent: the only condition that could trigger a wider read.
        return [_untimed("WAKE-ACK %s" % eid)]

    monkeypatch.setattr(worker, "_read_pane_turns", stub)
    worker.on_session_end(**_payload("noop", turn_id=None, completed=False))

    assert calls, "the sweep must still read the pane once"
    assert all("window" not in kw for kw in calls), \
        "never pass --window to a readback that does not support it: %s" % calls
    assert len(sends) == 1, "refusal must never resend"
