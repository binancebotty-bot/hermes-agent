"""DELIVERY_PENDING must settle through the existing reconciliation sweep.

A slow sender can be killed by the hook budget after the wake actually landed;
the GPT then replies WAKE-ACK. The next hook invocation must settle that event
from the pane through the same bounded sweep used for other open deliveries.
It must never send again: an attempt row means a send may already have happened.
"""
from __future__ import annotations

from test_hook_budget import _counting_sender, _load, _payload, _rows, _worker

EVENT_ID = "426db9ee-d4e8-4102-b306-602aef6a35e7"
OLD_ID = "11111111-1111-1111-1111-111111111111"
UTC = "2026-11-22T00:00:00+00:00"
ACK_UTC = "2026-11-22T00:00:05+00:00"


def _acked_turns(event_id=EVENT_ID):
    """Delivery turn plus a timestamped ACK, as a real readback record would."""
    return [{"text": f"EVENT-ID: {event_id}", "utc": UTC},
            {"text": f"WAKE-ACK {event_id}", "utc": ACK_UTC}]


def _pending_worker(tmp_path, monkeypatch, *, with_attempt=True):
    """Journal one event left at DELIVERY_PENDING on a previous invocation."""
    worker, journal = _worker(tmp_path, _load())
    worker._append({"kind": "event", "event_id": EVENT_ID, "utc": UTC,
                    "dedupe_key": "none:at-least-once",
                    "payload": {"task_id": "cron:bd8397ec957d:1"}})
    if with_attempt:
        worker._append({"kind": "attempt", "event_id": EVENT_ID, "utc": UTC,
                        "outcome": "ATTEMPTED", "argv": ["python", "sender.py", EVENT_ID]})
    worker._append({"kind": "outcome", "event_id": EVENT_ID, "utc": UTC,
                    "outcome": "DELIVERY_PENDING"})
    sends = _counting_sender(worker, monkeypatch)
    return worker, journal, sends


def _latest(journal, event_id=EVENT_ID):
    outcomes = [row for row in _rows(journal)
                if row.get("kind") == "outcome" and row.get("event_id") == event_id]
    return outcomes[-1] if outcomes else None


def _invoke(worker):
    """A later invocation with no turn_id: only the reconciliation sweep runs."""
    return worker.on_session_end(**_payload(turn_id=None, completed=False))


def test_a_pending_with_ack_token_becomes_delivered_acked_with_ref(tmp_path, monkeypatch):
    """(a) An observed WAKE-ACK settles the event as DELIVERED_ACKED with a ref."""
    worker, journal, sends = _pending_worker(tmp_path, monkeypatch)
    monkeypatch.setattr(worker, "_read_pane_turns", lambda lane, timeout: _acked_turns())
    _invoke(worker)
    row = _latest(journal)
    assert row["outcome"] == "DELIVERED_ACKED"
    assert row.get("ack_message_ref"), "the ack reference must be recorded"
    assert sends == [], "(e) reconciliation must never invoke the sender"


def test_b_pending_with_body_no_ack_becomes_delivered_unconfirmed(tmp_path, monkeypatch):
    """(b) A positively present nonce with no ack settles DELIVERED_UNCONFIRMED."""
    worker, journal, sends = _pending_worker(tmp_path, monkeypatch)
    monkeypatch.setattr(worker, "_read_pane_turns",
                        lambda lane, timeout: [f"EVENT-ID: {EVENT_ID}"])
    _invoke(worker)
    assert _latest(journal)["outcome"] == "DELIVERED_UNCONFIRMED"
    assert sends == [], "(e) reconciliation must never invoke the sender"


def test_c_pending_with_unreadable_pane_fails_closed(tmp_path, monkeypatch):
    """(c) An unreadable pane settles as RECONCILE_UNAVAILABLE, never delivered."""
    worker, journal, sends = _pending_worker(tmp_path, monkeypatch)

    def unreadable(lane, timeout):
        raise RuntimeError("pane unreadable")

    monkeypatch.setattr(worker, "_read_pane_turns", unreadable)
    _invoke(worker)
    assert _latest(journal)["outcome"] == "RECONCILE_UNAVAILABLE"
    assert sends == [], "(e) an unreadable pane must never invoke the sender"


def test_d_unsent_pending_with_similar_older_body_is_not_marked_delivered(tmp_path, monkeypatch):
    """(d) No attempt row, and only an older similar message: not delivered.

    Identity is the unique event id/nonce, never body wording.
    """
    worker, journal, sends = _pending_worker(tmp_path, monkeypatch, with_attempt=False)
    older = (f"EVENT-ID: {OLD_ID} WAKE from Hermes (hermes:mcp-integration). "
             f"Reply with this exact line and nothing else: WAKE-ACK {OLD_ID}")
    monkeypatch.setattr(worker, "_read_pane_turns", lambda lane, timeout: [older])
    _invoke(worker)
    assert _latest(journal)["outcome"] == "DELIVERY_PENDING", \
        "an unrelated older message with similar words must not confirm delivery"
    assert sends == [], "(e) an unsent pending event must not be sent by reconciliation"


def test_hook_invocation_settles_pending_without_resending_its_id(tmp_path, monkeypatch):
    """A completed later invocation settles the old event; no send carries its id."""
    worker, journal, sends = _pending_worker(tmp_path, monkeypatch)
    monkeypatch.setattr(worker, "_read_pane_turns", lambda lane, timeout: _acked_turns())
    worker.on_session_end(**_payload(turn_id=None, completed=True))
    assert _latest(journal)["outcome"] == "DELIVERED_ACKED"
    assert all(EVENT_ID not in " ".join(argv) for argv in sends), \
        "no physical send may carry the already-attempted event id"
