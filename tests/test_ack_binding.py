"""W1c: ACK reconciliation must settle an event from the conversation and
generation it was DELIVERED to, not from one global lane.

Each test drives one real ``on_session_end`` delivery with a **stub sender**,
then a second invocation whose ``_read_pane_turns`` is a stub that records which
lane was read. No real conversation, transport, config or plugin tree is touched.

(a) ACK in the event's own bound conversation settles it.
(b) The exact nonce in a DIFFERENT conversation does not settle it.
(c) A mention that predates the delivery does not settle it.
(d) A reply lacking the nonce does not settle it.
(e) A rolled generation does not settle it, and records the truthful outcome.
(f) A global-fallback event still reconciles from the configured lane, labelled.
"""
from __future__ import annotations

import json

from test_binding import (
    CONV_A, CONV_B, LANE_A, LANE_B, GEN,
    _bind, _load, _outcomes, _payload, _rows, _worker,
)


def _delivered_worker(tmp_path, monkeypatch, task_id, conv, *,
                      global_lane=None, allow_fallback=False):
    """Deliver one event with a stub sender; return worker, journal, event_id."""
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter, allow_fallback=allow_fallback)
    if global_lane:
        worker.lane = global_lane
    sends = []

    def stub_sender(argv):
        sends.append(argv)
        return "DELIVERED_UNACKED"

    monkeypatch.setattr(worker, "_run_sender", stub_sender)
    if conv is not None:
        _bind(worker, task_id, conv=conv)
    assert worker.on_session_end(
        **_payload(task_id, turn_id="turn-" + task_id)) == "DELIVERED_UNACKED"
    event = [r for r in _rows(journal) if r.get("kind") == "event"][-1]
    return worker, journal, event["event_id"], sends


def _sweep(worker, monkeypatch, by_lane):
    """Second invocation: reconcile only, with a lane-aware stub pane read."""
    reads = []

    def stub_pane(lane, timeout, window=None):
        reads.append(lane)
        return list(by_lane.get(lane, []))

    monkeypatch.setattr(worker, "_read_pane_turns", stub_pane)
    result = worker.on_session_end(**_payload("noop", turn_id=None, completed=False))
    return reads, result


def _latest(journal, event_id):
    return _outcomes(journal, event_id)[-1]


def _evidence(journal, event_id, *reply_texts):
    """Pane records: delivery turn at the recorded utc, replies at/after it.

    W1d requires both the delivery turn in the window and a turn timestamp;
    the assertions below are unchanged, the fixture just carries that proof.
    """
    attempts = [r for r in _rows(journal) if r.get("kind") == "attempt"
                and r.get("event_id") == event_id]
    utc = attempts[-1]["utc"]
    records = [{"text": "EVENT-ID: " + event_id, "utc": utc}]
    records.extend({"text": text, "utc": utc} for text in reply_texts)
    return records


# --- (a) own bound conversation settles -----------------------------------

def test_a_ack_in_own_bound_conversation_settles(tmp_path, monkeypatch):
    """(a) An ACK in the event's own bound conversation settles it."""
    worker, journal, eid, sends = _delivered_worker(
        tmp_path, monkeypatch, "task-ack-a", CONV_B)
    reads, _ = _sweep(worker, monkeypatch,
                      {LANE_B: _evidence(journal, eid, "WAKE-ACK " + eid)})

    row = _latest(journal, eid)
    assert row["outcome"] == "DELIVERED_ACKED", \
        "ACK in the bound conversation must settle: got %s" % row["outcome"]
    assert reads == [LANE_B], \
        "reconciliation must read the bound lane, not the global lane A: %s" % reads
    assert len(sends) == 1, "reconciliation must never invoke the sender again"


# --- (b) same nonce in a different conversation is refused ----------------

def test_b_same_nonce_in_other_conversation_does_not_settle(tmp_path, monkeypatch):
    """(b) Delivery to A must not be settled by an ACK in B holding the nonce.

    Global lane is deliberately set to B: the old global-lane sweep reads B,
    sees the exact nonce and wrongly settles an event delivered to A.
    """
    worker, journal, eid, _ = _delivered_worker(
        tmp_path, monkeypatch, "task-ack-b", CONV_A, global_lane=LANE_B)
    reads, _ = _sweep(worker, monkeypatch, {
        LANE_B: ["WAKE-ACK " + eid],   # wrong conversation, exact nonce
        LANE_A: [],                    # the conversation it was delivered to
    })

    row = _latest(journal, eid)
    assert row["outcome"] != "DELIVERED_ACKED", \
        "an ACK in conversation B must never settle an event delivered to A"
    assert reads == [LANE_A], \
        "the bound lane A must be read, never the global lane B: %s" % reads


# --- (c) a mention predating the delivery is refused ----------------------

def test_c_mention_predating_delivery_does_not_settle(tmp_path, monkeypatch):
    """(c) An earlier mention of the same text cannot settle a later event.

    The pane shows the ACK text BEFORE the outbound delivery turn, so it
    predates this event's delivery. Global lane equals the bound lane here so
    the old sweep reads the right lane and still wrongly settles.
    """
    worker, journal, eid, _ = _delivered_worker(
        tmp_path, monkeypatch, "task-ack-c", CONV_B, global_lane=LANE_B)
    reads, _ = _sweep(worker, monkeypatch, {
        LANE_B: ["WAKE-ACK " + eid, "EVENT-ID: " + eid],
    })

    row = _latest(journal, eid)
    assert row["outcome"] != "DELIVERED_ACKED", \
        "an ACK turn before the delivery turn must not settle the event"
    assert reads == [LANE_B]


# --- (d) an ACK without the nonce is refused ------------------------------

def test_d_ack_without_nonce_does_not_settle(tmp_path, monkeypatch):
    """(d) A reply that does not carry the event nonce does not settle it."""
    worker, journal, eid, _ = _delivered_worker(
        tmp_path, monkeypatch, "task-ack-d", CONV_B)
    reads, _ = _sweep(worker, monkeypatch, {
        LANE_B: ["an assistant reply that never names the event"],
    })

    row = _latest(journal, eid)
    assert row["outcome"] != "DELIVERED_ACKED", \
        "a nonce-less reply must not settle the event"
    assert reads == [LANE_B], \
        "reconciliation must still read the bound lane: %s" % reads


# --- (e) generation safety on the ACK side --------------------------------

def test_e_rolled_generation_does_not_settle(tmp_path, monkeypatch):
    """(e) A bound conversation that rolled past the recorded generation is
    not a source of ACK evidence; the truthful outcome is recorded instead.
    """
    worker, journal, eid, _ = _delivered_worker(
        tmp_path, monkeypatch, "task-ack-e", CONV_B, global_lane=LANE_B)
    peer = json.loads(worker.peer_map_path.read_text(encoding="utf-8"))
    peer["lanes"][LANE_B]["generation"] = GEN + 1   # pane rotated after send
    worker.peer_map_path.write_text(json.dumps(peer), encoding="utf-8")

    reads, _ = _sweep(worker, monkeypatch, {LANE_B: ["WAKE-ACK " + eid]})

    row = _latest(journal, eid)
    assert row["outcome"] == "ACK_GENERATION_ROLLED", \
        "a rolled generation must be a truthful refusal, got %s" % row["outcome"]
    assert row.get("recorded_generation") == GEN
    assert row.get("observed_generation") == GEN + 1
    assert reads == [], \
        "a rolled generation must be refused before trusting its pane: %s" % reads


# --- (f) global fallback is labelled --------------------------------------

def test_f_global_fallback_reconciles_from_configured_lane_and_is_labelled(
        tmp_path, monkeypatch):
    """(f) An unbound event delivered under the explicit opt-in uses the
    configured lane and is labelled as a global-fallback ACK.
    """
    worker, journal, eid, sends = _delivered_worker(
        tmp_path, monkeypatch, "task-ack-f", None, allow_fallback=True)
    assert sends[0][2] == LANE_A
    reads, _ = _sweep(worker, monkeypatch,
                      {LANE_A: _evidence(journal, eid, "WAKE-ACK " + eid)})

    row = _latest(journal, eid)
    assert row["outcome"] == "DELIVERED_ACKED", \
        "global-fallback events must still reconcile, got %s" % row["outcome"]
    assert row.get("ack_lane_source") == "global_fallback", \
        "a global-fallback ACK must be labelled as such"
    assert row.get("ack_lane") == LANE_A
    assert reads == [LANE_A]
