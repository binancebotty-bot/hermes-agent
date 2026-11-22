"""W1h: close the five conditions from the ACK-binding review at 7e19c67.

(1) an ACK needs the event nonce AND the wake-ack keyword in the reply half;
(2) unproven ACK outcomes are bounded, never retried forever;
(3) outcome/ack_binding pairs are enforced;
(4) --status surfaces the ACK label and per-outcome counts;
(5) a combined readback block without the separator does not lose a real ACK.

Every fixture is a stub transport / synthesised pane record in a disposable
tmp_path. Nothing is installed, sent, configured or contacted.
"""
from __future__ import annotations

import json

from test_binding import CONV_B, GEN, _bind, _load, _outcomes, _payload, _rows, _worker
from test_wake_activate import build_home, status_json

KEYWORD_MISSING = "ACK_KEYWORD_MISSING"
UNPROVEN_EXPIRED = "ACK_UNPROVEN_EXPIRED"
PAIRING_INVALID = "ACK_PAIRING_INVALID"
SETTLED = ("DELIVERED_ACKED", "ACKED_ORDER_ONLY")


def _deliver(tmp_path, monkeypatch, task_id):
    """One real on_session_end delivery with a stub sender."""
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


def _sweep(worker, monkeypatch, turns):
    """One reconcile invocation over synthesised untimed pane turns."""
    def stub(lane, timeout, window=None):
        return list(turns)

    monkeypatch.setattr(worker, "_read_pane_turns", stub)
    return worker.on_session_end(**_payload("noop", turn_id=None, completed=False))


def _untimed(text):
    return {"text": text}


def _outcome_names(journal, eid):
    return [r.get("outcome") for r in _outcomes(journal, eid)]


# --- (1) nonce alone is not an ACK -----------------------------------------

def test_1_nonce_without_keyword_records_ack_keyword_missing(tmp_path, monkeypatch):
    """A reply carrying only the nonce must not settle and must be visible."""
    worker, journal, eid, sends = _deliver(tmp_path, monkeypatch, "w1h-keyword")
    turns = [_untimed("EVENT-ID: " + eid),
             _untimed("noted, will look at %s later" % eid)]
    _sweep(worker, monkeypatch, turns)

    names = _outcome_names(journal, eid)
    assert KEYWORD_MISSING in names, \
        "nonce without WAKE-ACK must record %s: %s" % (KEYWORD_MISSING, names)
    assert not any(name in SETTLED for name in names), \
        "a nonce-only reply must never settle: %s" % names
    assert len(sends) == 1, "refusal must never resend"


# --- (2) bounded unproven states -------------------------------------------

def test_2a_sweep_count_expires_delivery_unproven(tmp_path, monkeypatch):
    """The pre-empted branch: ACK_DELIVERY_UNPROVEN must still be able to expire."""
    worker, journal, eid, sends = _deliver(tmp_path, monkeypatch, "w1h-sweeps")
    worker.ack_unproven_max_sweeps = 2
    worker.ack_unproven_max_seconds = 1.0e9
    turns = [_untimed("WAKE-ACK " + eid)]  # reply, no delivery anchor

    for _ in range(5):
        _sweep(worker, monkeypatch, turns)

    names = _outcome_names(journal, eid)
    assert names[-1] == UNPROVEN_EXPIRED, \
        "a bounded number of unproven sweeps must expire terminally: %s" % names
    assert len(sends) == 1, "expiry must never resend"


def test_2b_elapsed_time_expires_unproven(tmp_path, monkeypatch):
    """An old unproven outcome expires on elapsed time, not sweep count."""
    worker, journal, eid, sends = _deliver(tmp_path, monkeypatch, "w1h-elapsed")
    worker.ack_unproven_max_sweeps = 99
    worker.ack_unproven_max_seconds = 1.0
    turns = [_untimed("WAKE-ACK " + eid)]
    _sweep(worker, monkeypatch, turns)

    # Synthesised fixture: age the unproven row past the named elapsed limit.
    stale = "2020-01-01T00:00:00Z"
    rows = _rows(journal)
    for row in rows:
        if row.get("kind") == "outcome" and row.get("outcome") == "ACK_DELIVERY_UNPROVEN":
            row["utc"] = stale
    journal.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    _sweep(worker, monkeypatch, turns)
    names = _outcome_names(journal, eid)
    assert names[-1] == UNPROVEN_EXPIRED, \
        "an unproven outcome past the elapsed limit must expire: %s" % names
    assert len(sends) == 1, "expiry must never resend"


# --- (3) outcome / ack_binding pairing -------------------------------------

def test_3_pairing_rejects_both_mismatched_directions(tmp_path, monkeypatch):
    """record_assistant_ack refuses ACKED_ORDER_ONLY/time_verified and reverse."""
    worker, journal, eid, _ = _deliver(tmp_path, monkeypatch, "w1h-pairing")
    before = len(_outcomes(journal, eid))

    bad_order = worker.record_assistant_ack(
        eid, outcome="ACKED_ORDER_ONLY", ack_binding="time_verified")
    bad_time = worker.record_assistant_ack(
        eid, outcome="DELIVERED_ACKED", ack_binding="order_only")

    assert bad_order == PAIRING_INVALID, \
        "order-only label with time_verified binding must be refused: %r" % (bad_order,)
    assert bad_time == PAIRING_INVALID, \
        "time-verified label with order_only binding must be refused: %r" % (bad_time,)
    assert len(_outcomes(journal, eid)) == before, \
        "a refused pair must not be recorded at all"


# --- (4) surface the label in --status ------------------------------------

def test_4a_emitter_status_reports_ack_label_and_outcome_counts(tmp_path, monkeypatch):
    """The liveness status record exposes labels and per-outcome counts."""
    worker, journal, eid1, _ = _deliver(tmp_path, monkeypatch, "w1h-status-a")
    assert worker.record_assistant_ack(
        eid1, outcome="ACKED_ORDER_ONLY", ack_binding="order_only") is True

    _bind(worker, "w1h-status-b", conv=CONV_B, generation=GEN)
    assert worker.on_session_end(
        **_payload("w1h-status-b", turn_id="turn-b")) == "DELIVERED_UNACKED"
    eid2 = [r for r in _rows(journal) if r.get("kind") == "event"][-1]["event_id"]
    assert worker.record_assistant_ack(eid2) is True

    worker._write_status()
    record = json.loads(worker.status_path.read_text(encoding="utf-8"))
    counts = record.get("outcome_counts", {})
    assert counts.get("ACKED_ORDER_ONLY") == 1, \
        "--status must count ACKED_ORDER_ONLY separately: %s" % counts
    assert counts.get("DELIVERED_ACKED") == 1, \
        "--status must count DELIVERED_ACKED separately: %s" % counts
    assert record.get("ack_label") == "DELIVERED_ACKED", \
        "--status must show the latest ACK label: %s" % record
    assert "ack_unproven_max_sweeps" in record and "ack_unproven_max_seconds" in record, \
        "the named unproven limits must be reported in status: %s" % record


def test_4b_cli_status_surfaces_emitter_ack(tmp_path):
    """python wake_activate.py --status shows the ACK label and counts."""
    fx = build_home(tmp_path)
    status_file = fx.journal.with_name("event_outbox.status.json")
    status_file.write_text(json.dumps({
        "ack_label": "ACKED_ORDER_ONLY",
        "outcome_counts": {"ACKED_ORDER_ONLY": 1, "DELIVERED_ACKED": 2},
        "ack_unproven_max_sweeps": 5,
        "ack_unproven_max_seconds": 900.0,
    }), encoding="utf-8")

    st = status_json(fx)
    ack = st.get("emitter_ack", {})
    assert ack.get("ack_label") == "ACKED_ORDER_ONLY", \
        "--status must surface the ACK label: %s" % ack
    assert ack.get("outcome_counts", {}).get("ACKED_ORDER_ONLY") == 1, \
        "--status must surface per-outcome counts: %s" % ack
    assert ack.get("outcome_counts", {}).get("DELIVERED_ACKED") == 2, \
        "--status must surface per-outcome counts: %s" % ack


# --- (5) combined block without the separator -----------------------------

def test_5_combined_block_without_separator_keeps_ack(tmp_path, monkeypatch):
    """Envelope marker + keyword ACK line in one block still settles."""
    worker, journal, eid, sends = _deliver(tmp_path, monkeypatch, "w1h-combined")
    combined = "EVENT-ID: %s\nWAKE-ACK %s" % (eid, eid)
    _sweep(worker, monkeypatch, [_untimed(combined)])

    names = _outcome_names(journal, eid)
    assert any(name in SETTLED for name in names), \
        "a keyword ACK line inside a combined block must not be lost: %s" % names
    assert len(sends) == 1, "settlement must never resend"


def test_5b_envelope_only_block_is_still_refused(tmp_path, monkeypatch):
    """Regression guard: our own envelope alone never acknowledges anything."""
    worker, journal, eid, _ = _deliver(tmp_path, monkeypatch, "w1h-envelope-only")
    envelope = ("EVENT-ID: %s WAKE from Hermes (hermes:mcp-integration). "
                "Reply with this exact line and nothing else: WAKE-ACK %s" % (eid, eid))
    _sweep(worker, monkeypatch, [_untimed(envelope)])

    names = _outcome_names(journal, eid)
    assert not any(name in SETTLED for name in names), \
        "our own envelope must never settle an event: %s" % names
