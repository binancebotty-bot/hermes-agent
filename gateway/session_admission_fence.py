"""Conditional admission for ``prompt.submit`` (Option C, RC-11b15f5b).

The caller states the turn it observed; the owner admits only if that observation is still current. The
comparison runs inside the session's mutation lock, so it is decided atomically with respect to any other
admission for the same session: a prompt that would otherwise be silently folded into a turn somebody else
started is refused by name instead, and no text is inserted.

Why this exists: a submit sent while an unrelated turn was starting came back ``steered`` - the text was
absorbed into a turn the caller never opened and no user row was ever written for it. Neither looking idle
before sending nor retrying afterwards can close that window; only the owner can, at admission time.

Refusal vocabulary (stable, so a caller can act without guessing):

``stale_turn_refused``
    The observed turn is no longer current (a newer turn started, finished or was cancelled). Nothing was
    admitted, nothing was modified. The caller may re-read the session and submit again.
``seat_busy``
    The observed turn is still the current one and it is running. The seat is genuinely busy, so the text is
    refused explicitly (Option A) instead of being steered into that turn.
``None``
    Admit: the observation is current and no turn is running, or the caller sent no expectation at all
    (unchanged legacy behaviour).

Deliberately NOT here: any notion of "maybe". A steer that cannot be verified is never labelled as admitted
by this gate - that decision belongs to the runner, and the caller treats an unverifiable steer as unverified
(``REFUSED_STEERED`` on the push path).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

__all__ = ["FENCE_PARAMS", "evaluate", "normalize_expectation"]

#: The only request fields this gate consumes. They are admission metadata and never reach the transcript.
FENCE_PARAMS = ("expect_turn", "expect_generation")

STALE_TURN_REFUSED = "stale_turn_refused"
SEAT_BUSY = "seat_busy"


def normalize_expectation(params: Mapping[str, Any]) -> Optional[dict]:
    """Pull the caller's observation out of a request, or ``None`` when it sent none.

    Both numbers are non-negative integers or the request is malformed: an unparsable expectation must not
    quietly degrade to "no expectation", which would admit exactly the case this gate exists to refuse.
    """
    raw_turn = params.get("expect_turn")
    raw_gen = params.get("expect_generation")
    if raw_turn is None and raw_gen is None:
        return None
    out: dict[str, int] = {}
    for key, value in (("expect_turn", raw_turn), ("expect_generation", raw_gen)):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("invalid_expectation:%s" % key)
        out[key] = value
    return out


def evaluate(expect: Optional[Mapping[str, Any]], *, generation: int, state: str) -> Optional[str]:
    """Decide admission for an observation against the owner's live state.

    ``generation`` is the session's live execution generation and ``state`` its live execution state, both
    read by the owner under the session mutation lock - never inferred by the caller.
    """
    if not expect:
        return None
    expected_generation = expect.get("expect_generation")
    expected_turn = expect.get("expect_turn")
    # ``expect_turn`` and ``expect_generation`` address the same counter: the generation is the turn identity.
    for value in (expected_generation, expected_turn):
        if value is not None and value != generation:
            return STALE_TURN_REFUSED
    if state == "running":
        return SEAT_BUSY
    return None
