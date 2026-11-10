"""Conditional admission (Option C, RC-11b15f5b): the six acceptance cases.

Each test drives the fence the way the owner does - decision taken against a live (generation, state) pair read
under the session lock - and asserts the property the controller asked for, including that a refused submit
inserts no text at all.
"""

import asyncio
import threading

import pytest

from gateway.session_admission_fence import (FENCE_PARAMS, SEAT_BUSY, STALE_TURN_REFUSED, evaluate,
                                             normalize_expectation)


class Transcript:
    """Stands in for the session's committed rows: admission appends, refusal must not."""

    def __init__(self):
        self.rows = []

    def admit(self, text, generation):
        self.rows.append({"text": text, "generation": generation})
        return len(self.rows)

    def texts(self):
        return [r["text"] for r in self.rows]


class Owner:
    """The authority's admission gate: observation compared under the session mutation lock."""

    def __init__(self, generation=7, state="idle"):
        self.generation = generation
        self.state = state
        self.lock = threading.Lock()
        self.transcript = Transcript()

    def submit(self, text, expect):
        with self.lock:                       # the real gate runs inside the session's mutation lock
            refusal = evaluate(expect, generation=self.generation, state=self.state)
            if refusal:
                return {"status": "refused", "reason": refusal, "row": None}
            row = self.transcript.admit(text, self.generation)
            # Admitting opens a turn: the session's generation advances, which is exactly what makes another
            # submitter's observation stale from that instant on.
            self.generation += 1
            self.state = "running"
            return {"status": "admitted", "reason": None, "row": row}


# 1 ------------------------------------------------------------------ competing turn: one wins, zero absorbed
def test_competing_turn_race_one_wins_loser_refused_and_nothing_absorbed():
    owner = Owner(generation=7, state="idle")
    observed = {"expect_turn": 7, "expect_generation": 7}

    results = []
    barrier = threading.Barrier(2)

    def submitter(text):
        barrier.wait()
        results.append(owner.submit(text, observed))

    threads = [threading.Thread(target=submitter, args=(t,)) for t in ("A", "B")]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    admitted = [r for r in results if r["status"] == "admitted"]
    refused = [r for r in results if r["status"] == "refused"]
    assert len(admitted) == 1, results
    assert len(refused) == 1, results
    assert refused[0]["reason"] in (STALE_TURN_REFUSED, SEAT_BUSY), refused
    assert len(owner.transcript.rows) == 1, owner.transcript.texts()   # exactly one physical insert
    assert refused[0]["row"] is None


def test_stale_observation_is_refused_by_name():
    owner = Owner(generation=9, state="idle")          # a newer turn already ran
    out = owner.submit("late", {"expect_generation": 7, "expect_turn": 7})
    assert out == {"status": "refused", "reason": STALE_TURN_REFUSED, "row": None}
    assert owner.transcript.texts() == []


# 2 ------------------------------------------------------------------ busy seat: explicit refusal, turn untouched
def test_busy_seat_refuses_explicitly_and_modifies_nothing():
    owner = Owner(generation=7, state="running")
    before = list(owner.transcript.rows)
    out = owner.submit("steer me", {"expect_generation": 7, "expect_turn": 7})
    assert out["status"] == "refused" and out["reason"] == SEAT_BUSY
    assert owner.transcript.rows == before          # the active turn is untouched
    assert out["row"] is None


# 3 ------------------------------------------------------------------ idle seat: correlated row id returned
def test_idle_seat_admits_and_returns_correlated_row():
    owner = Owner(generation=7, state="idle")
    out = owner.submit("hello", {"expect_generation": 7})
    assert out["status"] == "admitted"
    assert out["row"] == 1
    assert owner.transcript.texts() == ["hello"]


# 4 ------------------------------------------------------------------ crash between admission and row write
def test_crash_between_admission_and_row_write_leaves_no_duplicate():
    owner = Owner(generation=7, state="idle")
    out = owner.submit("crashy", {"expect_generation": 7})
    assert out["row"] == 1
    # The owner died before the row was readable; the caller retries the SAME observation.
    owner.state = "idle"
    owner.generation = 8            # the owner restart advanced the generation: the old observation is stale
    retry = owner.submit("crashy", {"expect_generation": 7})
    assert retry == {"status": "refused", "reason": STALE_TURN_REFUSED, "row": None}
    assert owner.transcript.texts() == ["crashy"]      # no duplicate physical insert


# 5 ------------------------------------------------------------------ rollover mid-flight
def test_rollover_mid_flight_never_duplicates_and_never_steers():
    owner = Owner(generation=7, state="running")
    out = owner.submit("during roll", {"expect_generation": 7})
    assert out["reason"] == SEAT_BUSY                  # refused, not steered into the turn
    assert owner.transcript.texts() == []
    # after the roll the session is a different one with its own generation: the old observation is stale
    rolled = Owner(generation=1, state="idle")
    assert rolled.submit("during roll", {"expect_generation": 7})["reason"] == STALE_TURN_REFUSED
    assert rolled.transcript.texts() == []


# 6 ------------------------------------------------------------------ legacy callers unchanged; params validated
def test_no_expectation_keeps_legacy_behaviour():
    owner = Owner(generation=7, state="idle")
    out = owner.submit("legacy", None)
    assert out["status"] == "admitted"
    assert normalize_expectation({}) is None
    assert normalize_expectation({"text": "x"}) is None


def test_malformed_expectation_is_not_silently_ignored():
    with pytest.raises(ValueError):
        normalize_expectation({"expect_turn": "seven"})
    with pytest.raises(ValueError):
        normalize_expectation({"expect_generation": -1})
    with pytest.raises(ValueError):
        normalize_expectation({"expect_turn": True})


def test_fence_params_are_the_only_metadata_fields():
    assert FENCE_PARAMS == ("expect_turn", "expect_generation")
