"""Real SessionDB/finish_result RED-first P0 regression.

All writes use tmp_path. No real transport, ChatGPT pane or production database.
The positive test needs an explicit TRUSTED in-process completion proof and a
fresh, separately verified recipient binding. A model's response string or
synthetic result alone must never qualify as a P0 completion.
"""
import sqlite3

import pytest

from gateway.session_results import finish_result
from hermes_state import SessionDB
from hermes_state_runtime import (
    RuntimeStoreError, admit_session_input, begin_runtime_epoch,
    claim_session_input, get_session_admission, settle_session_input,
)

SID = "p0-nonprod-session"
ROLE = "gpt6:external-monitor"


@pytest.fixture
def admission(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(SID, source="test")
    try:
        epoch = begin_runtime_epoch(db, instance_id="p0-test-owner")
        admit_session_input(
            db, epoch=epoch, principal_id="test-principal",
            session_id=SID, request_id="p0-request",
            payload={"text": "complete the admitted test turn"},
        )
        claimed = claim_session_input(db, epoch=epoch, session_id=SID)
        assert claimed is not None and claimed["status"] == "started"
        yield db, epoch, claimed
    finally:
        db.close()


def captured_model_result():
    return {
        "result": {"final_response": "done", "messages": [], "completed": True},
        "usage": {},
    }


def pending_obligations(db):
    # The pre-fix source has no table. Absence means zero, not success.
    table = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='p0_completion_obligations'"
    ).fetchone()
    if table is None:
        return []
    return db._conn.execute(
        "SELECT admission_id,source_epoch,source_generation,destination_role,state "
        "FROM p0_completion_obligations ORDER BY admission_id"
    ).fetchall()


def settle(db, epoch, row, *, result=None):
    return finish_result(
        db, epoch=epoch, row=row, response="done",
        outcome="completed", result=result or captured_model_result(),
    )


def test_future_real_producer_must_install_hook_for_eligible_terminal(admission):
    """Intentional RED until the REAL gateway caller wires the same-conn hook."""
    db, epoch, row = admission
    # This binding is TEST-INJECTED and intentionally carries no live identity.
    # Future code must take its equivalent only from authenticated gateway state.
    destination = {
        "destination_role": ROLE,
        "destination_route": "nonphysical-test-route",
        "destination_generation": 17,
    }
    finish_result(
        db, epoch=epoch, row=row, response="done", outcome="completed",
        result=captured_model_result(),
        trusted_execution=True, p0_target=destination,
    )
    rows = pending_obligations(db)
    assert len(rows) == 1, "Missing P0 OPEN on the first genuine terminal write"
    assert tuple(rows[0]) == (
        row["admission_id"], epoch, row["generation"], ROLE, "OPEN"
    )
    assert get_session_admission(db, admission_id=row["admission_id"])["status"] == "terminal"


def test_plain_result_without_trusted_gateway_provenance_is_not_a_wake(admission):
    db, epoch, row = admission
    settle(db, epoch, row)
    assert pending_obligations(db) == []


def test_synthetic_claimed_execution_is_not_authentic_evidence(admission):
    db, epoch, row = admission
    # No trusted gateway flag or verified target. Model/text cannot grant either.
    settle(db, epoch, row, result={
        "result": {"final_response": "synthetic claimed execution", "completed": True},
        "synthetic_claimed_execution": True, "usage": {},
    })
    assert pending_obligations(db) == []


def test_stale_generation_fails_without_creating_obligation(admission):
    db, epoch, row = admission
    with pytest.raises(RuntimeStoreError, match="stale_generation"):
        settle_session_input(
            db, epoch=epoch, admission_id=row["admission_id"],
            generation=row["generation"] + 1, outcome="completed",
        )
    assert pending_obligations(db) == []
    assert get_session_admission(db, admission_id=row["admission_id"])["status"] == "started"


def test_replay_of_settled_untrusted_result_cannot_add_obligation(admission):
    db, epoch, row = admission
    settle(db, epoch, row)
    settle(db, epoch, row)
    assert pending_obligations(db) == []
    assert get_session_admission(db, admission_id=row["admission_id"])["status"] == "terminal"
