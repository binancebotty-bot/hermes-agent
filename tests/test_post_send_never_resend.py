"""Once a write-ahead attempt row exists, a send may already have happened.

A pane readback is evidence of presence, never of absence: a readable tail that
omits the nonce proves only that we cannot tell. These tests pin the four
post/pre-send fault cases and the happy path to at most one physical send per
event.
"""
from __future__ import annotations

import json

from test_internal_error_nonterminal import (
    CONV, LANE, _load, _payload, _rows, _worker, _post_send_fault_worker,
)


def _counting_sender(worker, monkeypatch):
    sends = []
    real_run = worker._run_sender

    def counting_run(argv):
        sends.append(argv)
        return real_run(argv)

    monkeypatch.setattr(worker, "_run_sender", counting_run)
    return sends


def test_a_post_send_fault_tail_without_nonce_is_unproven_not_resent(tmp_path, monkeypatch):
    """(a) The missing middle case: readback succeeds but its tail omits the
    nonce. That is not proof of absence, so the event stays open as
    RECONCILE_UNPROVEN and the sender is invoked exactly once across both calls.
    """
    worker, journal, real_record, sends, _event_id = _post_send_fault_worker(tmp_path, monkeypatch)
    monkeypatch.setattr(worker, "_record", real_record)
    monkeypatch.setattr(worker, "_read_pane_turns",
                        lambda lane, timeout: ["an assistant reply that never names the event"])
    result = worker.on_session_end(**_payload())
    assert len(sends) == 1, \
        "a tail without the nonce is not proof of absence: got %d physical sends" % len(sends)
    assert result == "RECONCILE_UNPROVEN"
    rows = _rows(journal)
    assert not any(row.get("kind") == "attempt" and row.get("outcome") == "RESENT"
                   for row in rows), "no second send intent may be written"
    last = [row for row in rows if row.get("kind") == "outcome"][-1]
    assert last["outcome"] == "RECONCILE_UNPROVEN"
    assert last["lane"] == LANE
    assert last["turns_read"] == 1


def test_unproven_stays_open_and_settles_without_resend(tmp_path, monkeypatch):
    """An ambiguous event stays open: a later readback that positively confirms
    the body settles it as DELIVERED_UNCONFIRMED, still with no second send.
    """
    worker, _journal, real_record, sends, event_id = _post_send_fault_worker(tmp_path, monkeypatch)
    monkeypatch.setattr(worker, "_record", real_record)
    monkeypatch.setattr(worker, "_read_pane_turns",
                        lambda lane, timeout: ["no nonce yet"])
    assert worker.on_session_end(**_payload()) == "RECONCILE_UNPROVEN"
    assert len(sends) == 1
    monkeypatch.setattr(worker, "_read_pane_turns",
                        lambda lane, timeout: [f"EVENT-ID: {event_id}"])
    assert worker.on_session_end(**_payload()) == "DELIVERED_UNCONFIRMED"
    assert len(sends) == 1, "settling an open event must not send"


def test_b_post_send_fault_body_present_is_delivered_unconfirmed(tmp_path, monkeypatch):
    """(b) A body positively present in the readback is DELIVERED_UNCONFIRMED,
    with no resend.
    """
    worker, _journal, real_record, sends, event_id = _post_send_fault_worker(tmp_path, monkeypatch)
    monkeypatch.setattr(worker, "_record", real_record)
    monkeypatch.setattr(worker, "_read_pane_turns",
                        lambda lane, timeout: [f"EVENT-ID: {event_id}"])
    assert worker.on_session_end(**_payload()) == "DELIVERED_UNCONFIRMED"
    assert len(sends) == 1, "a confirmed body must never produce a second physical send"


def test_c_post_send_fault_unreadable_pane_fails_closed(tmp_path, monkeypatch):
    """(c) An unreadable pane fails closed with RECONCILE_UNAVAILABLE, no resend.
    """
    worker, _journal, real_record, sends, _event_id = _post_send_fault_worker(tmp_path, monkeypatch)
    monkeypatch.setattr(worker, "_record", real_record)

    def unreadable(lane, timeout):
        raise RuntimeError("pane unreadable")

    monkeypatch.setattr(worker, "_read_pane_turns", unreadable)
    assert worker.on_session_end(**_payload()) == "RECONCILE_UNAVAILABLE"
    assert len(sends) == 1, "an unreadable pane must fail closed without a resend"


def test_d_pre_send_fault_without_attempt_row_delivers_once(tmp_path, monkeypatch):
    """(d) No attempt row means no send can have happened, so the fault stays
    retry-eligible and the next call delivers exactly once.
    """
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter, peer_map=[])
    sends = _counting_sender(worker, monkeypatch)
    assert worker.on_session_end(**_payload()) == "INTERNAL_ERROR:AttributeError"
    assert sends == [], "a pre-send fault must not have invoked the sender"
    assert not any(row.get("kind") == "attempt" for row in _rows(journal)), \
        "the fault happened before any send intent existed"
    worker.peer_map_path.write_text(
        json.dumps({"lanes": {LANE: {"conv": CONV, "generation": 1}}}), encoding="utf-8")
    assert worker.on_session_end(**_payload()) == "DELIVERED_UNACKED"
    assert len(sends) == 1, "the retry must deliver exactly once"


def test_e_happy_path_is_unchanged(tmp_path, monkeypatch):
    """(e) The normal completed turn still journals event + one ATTEMPTED send.
    """
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    sends = _counting_sender(worker, monkeypatch)
    assert worker.on_session_end(**_payload()) == "DELIVERED_UNACKED"
    assert len(sends) == 1
    rows = _rows(journal)
    assert [row["kind"] for row in rows] == ["event", "attempt", "outcome"]
    assert [row["outcome"] for row in rows[1:]] == ["ATTEMPTED", "DELIVERED_UNACKED"]
