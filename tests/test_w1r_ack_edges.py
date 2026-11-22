"""W1r: two false negatives in the ACK matcher. Staging only; stub transports.

Reported at cac8395 (PASS-WITH-CONDITIONS):
  1 a single turn holding our envelope AND the peer reply after a
    ``ChatGPT said:`` separator was discarded, so a real ACK never settled;
  2 a peer quoting our instruction line and then complying was discarded.

Both come from one rule: the envelope scan looked at the WHOLE turn, so our
outbound markers in the outbound half could mask a genuine reply half. The
reply half is now the only judged block, and our own payload must still never
settle (itself): the regression tests below are the pre-existing hole.
Nothing here installs, sends, configures, restarts or contacts an agent.
"""
from __future__ import annotations

from test_binding import CONV_B, GEN, _load
from test_w1h_ack_settlement import SETTLED, _deliver, _outcome_names, _sweep, _untimed

EID = "11111111-1111-4111-8111-111111111111"
SEP = "ChatGPT said:"
ACK = "WAKE-ACK " + EID
# The REAL payload _sender_argv writes: two lines, one turn.
TWO_LINE = (
    "EVENT-ID: %s\n"
    "WAKE from Hermes (hermes:mcp-integration). Reply with this exact line "
    "and nothing else: WAKE-ACK %s" % (EID, EID)
)
ONE_LINE = "RC-w1r-%s | EVENT-ID: %s WAKE from Hermes (hermes:mcp-integration)" % (EID, EID)
LINE2 = ("WAKE from Hermes (hermes:mcp-integration). Reply with this exact line "
         "and nothing else: WAKE-ACK " + EID)

COMBINED = TWO_LINE + "\n\n" + SEP + "\n" + ACK
QUOTED = "> " + TWO_LINE.replace("\n", "\n> ") + "\n\n" + SEP + "\n" + ACK


# --- finding 1: one turn holding both halves must settle -------------------

def test_1a_combined_envelope_plus_reply_is_an_ack_candidate():
    """Unit: our envelope, the separator, then a genuine reply line."""
    emitter = _load()
    assert emitter._ack_candidate(COMBINED, EID) is True, \
        "a combined block with a genuine reply half must be an ACK candidate"
    assert emitter._ack_nonce_without_keyword(COMBINED, EID) is False


def test_1b_combined_envelope_plus_reply_settles(tmp_path, monkeypatch):
    """Sweep: the same combined turn is the delivery anchor AND the ACK."""
    worker, journal, eid, sends = _deliver(tmp_path, monkeypatch, "w1r-combined")
    _sweep(worker, monkeypatch, [_untimed(
        "EVENT-ID: %s\nWAKE from Hermes (hermes:mcp-integration). Reply with this "
        "exact line and nothing else: WAKE-ACK %s\n\n%s\nWAKE-ACK %s"
        % (eid, eid, SEP, eid))])
    names = _outcome_names(journal, eid)
    assert any(name in SETTLED for name in names), \
        "a combined envelope-plus-reply turn must settle: %s" % names
    assert len(sends) == 1, "settlement must never resend"


def test_1c_combined_reply_after_a_delivery_turn_settles(tmp_path, monkeypatch):
    """Sweep: delivery turn, then one turn with our quote and the reply."""
    worker, journal, eid, sends = _deliver(tmp_path, monkeypatch, "w1r-two-turns")
    _sweep(worker, monkeypatch, [
        _untimed("EVENT-ID: " + eid),
        _untimed("EVENT-ID: %s\nWAKE from Hermes (hermes:mcp-integration)\n\n%s\nWAKE-ACK %s"
                 % (eid, SEP, eid)),
    ])
    names = _outcome_names(journal, eid)
    assert any(name in SETTLED for name in names), names
    assert len(sends) == 1


# --- finding 2: a peer quoting our instruction line, then complying --------

def test_2a_quoted_instruction_then_reply_is_an_ack_candidate():
    """Unit: the quote carries EVENT-ID:, the reply half is the peer's own line."""
    emitter = _load()
    assert emitter._ack_candidate(QUOTED, EID) is True, \
        "a peer quote of our envelope must not veto its genuine reply half"


def test_2b_quoted_instruction_then_reply_settles(tmp_path, monkeypatch):
    """Sweep: quoted envelope in the outbound half still settles."""
    worker, journal, eid, sends = _deliver(tmp_path, monkeypatch, "w1r-quoted")
    _sweep(worker, monkeypatch, [
        _untimed("EVENT-ID: " + eid),
        _untimed("> EVENT-ID: %s\n\n%s\nWAKE-ACK %s" % (eid, SEP, eid)),
    ])
    names = _outcome_names(journal, eid)
    assert any(name in SETTLED for name in names), names
    assert len(sends) == 1


# --- the self-ACK hole: our own payload must still never settle ------------

SELF_ACK_VARIANTS = {
    "two_line_payload": TWO_LINE,
    "one_line_payload": ONE_LINE,
    "line_2_alone": LINE2,
    "quoted_back_two_line_after_separator": TWO_LINE + "\n\n" + SEP + "\n" + TWO_LINE,
    "quoted_back_line_2_after_separator": TWO_LINE + "\n\n" + SEP + "\n" + LINE2,
    "line_2_then_empty_reply": LINE2 + "\n\n" + SEP + "\n",
    "two_line_then_bare_separator": TWO_LINE + "\n\n" + SEP,
    "line_2_plus_reply_instruction_only": LINE2 + "\n" + LINE2,
    "nonce_then_separator_then_line_2": "EVENT-ID: %s\n\n%s\n%s" % (EID, SEP, LINE2),
    "two_line_then_separator_then_nonce_echo": TWO_LINE + "\n\n" + SEP + "\nEVENT-ID: " + EID,
}


def test_3_every_self_ack_variant_stays_false():
    """Each variant of our own payload must never be read as a peer ACK."""
    emitter = _load()
    bad = {name: (emitter._ack_candidate(text, EID),
                  emitter._ack_without_separator(text))
           for name, text in SELF_ACK_VARIANTS.items()
           if emitter._ack_candidate(text, EID) or emitter._ack_without_separator(text)}
    assert not bad, "our own payload must never settle: %s" % bad


def test_3b_quoted_back_payload_never_settles_in_a_sweep(tmp_path, monkeypatch):
    """Sweep: a pane holding only our payload (quoted back) never settles."""
    worker, journal, eid, sends = _deliver(tmp_path, monkeypatch, "w1r-self-ack")
    _sweep(worker, monkeypatch, [
        _untimed("EVENT-ID: " + eid),
        _untimed("EVENT-ID: %s\nWAKE from Hermes (hermes:mcp-integration). Reply with "
                 "this exact line and nothing else: WAKE-ACK %s\n\n%s\nEVENT-ID: %s"
                 % (eid, eid, SEP, eid)),
    ])
    names = _outcome_names(journal, eid)
    assert not any(name in SETTLED for name in names), \
        "a self-ACK block must never settle: %s" % names
    assert len(sends) == 1


def test_4_genuine_reply_only_still_settles(tmp_path, monkeypatch):
    """Control: the plain two-turn case is untouched."""
    worker, journal, eid, sends = _deliver(tmp_path, monkeypatch, "w1r-control")
    _sweep(worker, monkeypatch, [_untimed("EVENT-ID: " + eid), _untimed("WAKE-ACK " + eid)])
    names = _outcome_names(journal, eid)
    assert any(name in SETTLED for name in names), names
    assert len(sends) == 1
