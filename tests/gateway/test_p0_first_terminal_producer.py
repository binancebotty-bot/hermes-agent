"""Real SessionDB/finish_result RED-first P0 regression.

All writes use tmp_path. No real transport, ChatGPT pane or production database.
The positive test needs an explicit TRUSTED in-process completion proof and a
fresh, separately verified recipient binding. A model's response string or
synthetic result alone must never qualify as a P0 completion.
"""
import sqlite3

import pytest

from gateway.session_results import (
    finish_result, execution_result, mark_in_process_execution, admitted_execution_proof,
)
from gateway.session_settlement_recovery import commit_and_publish
from types import SimpleNamespace
import threading
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


def real_runner_proof(epoch, row):
    # Mimics the private witness only real TurnRunner.run_sync installs in production.
    captured = {}
    token = execution_result.set(captured)
    try:
        mark_in_process_execution()
    finally:
        execution_result.reset(token)
    return admitted_execution_proof(captured, epoch=epoch, row=row)


def pending_obligations(db):
    # The pre-fix source has no table. Absence means zero, not success.
    table = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='p0_completion_obligations'"
    ).fetchone()
    if table is None:
        return []
    return db._conn.execute(
        "SELECT admission_id,source_epoch,source_generation,state "
        "FROM p0_completion_obligations ORDER BY admission_id"
    ).fetchall()


def settle(db, epoch, row, *, result=None):
    return finish_result(
        db, epoch=epoch, row=row, response="done",
        outcome="completed", result=result or captured_model_result(),
    )


def test_future_real_producer_must_install_hook_for_eligible_terminal(admission):
    """Real completion writes only immutable source coordinates and OPEN state."""
    db, epoch, row = admission
    finish_result(
        db, epoch=epoch, row=row, response="done", outcome="completed",
        result=captured_model_result(),
        trusted_execution=real_runner_proof(epoch, row),
    )
    rows = pending_obligations(db)
    assert len(rows) == 1, "Missing P0 OPEN on the first genuine terminal write"
    assert tuple(rows[0]) == (
        row["admission_id"], epoch, row["generation"], "OPEN"
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


def test_forged_true_flag_cannot_create_p0_obligation(admission):
    db, epoch, row = admission
    synthetic = {
        'result': {'final_response': 'nothing executed', 'completed': True},
        'synthetic_claimed_execution': True, 'usage': {},
    }
    finish_result(
        db, epoch=epoch, row=row, response='done', outcome='completed',
        result=synthetic, trusted_execution=True,
    )
    assert pending_obligations(db) == []


def test_missing_real_execution_proof_is_fail_closed(admission):
    db, epoch, row = admission
    assert admitted_execution_proof(captured_model_result(), epoch=epoch, row=row) is None
    settle(db, epoch, row)
    assert pending_obligations(db) == []


def test_cross_generation_execution_proof_is_refused(admission):
    db, epoch, row = admission
    wrong = {**row, 'generation': row['generation'] + 1}
    finish_result(
        db, epoch=epoch, row=row, response='done', outcome='completed',
        result=captured_model_result(), trusted_execution=real_runner_proof(epoch, wrong),
    )
    assert pending_obligations(db) == []


@pytest.mark.parametrize('outcome', ['failed', 'interrupted', 'rejected'])
def test_no_p0_for_noncompleted_terminal_even_with_proof(admission, outcome):
    db, epoch, row = admission
    finish_result(
        db, epoch=epoch, row=row, response='done', outcome=outcome,
        result=captured_model_result(), trusted_execution=real_runner_proof(epoch, row),
    )
    assert pending_obligations(db) == []


def test_positive_proof_replay_is_one_obligation(admission):
    db, epoch, row = admission
    proof = real_runner_proof(epoch, row)
    for _ in range(2):
        finish_result(
            db, epoch=epoch, row=row, response='done', outcome='completed',
            result=captured_model_result(), trusted_execution=proof,
        )
    assert len(pending_obligations(db)) == 1


def test_real_commit_and_publish_writes_source_only_open(admission):
    db, epoch, row = admission
    published = []
    live = SimpleNamespace(
        event_stream=SimpleNamespace(lock=threading.RLock(),
                                     publish=lambda sid, data: published.append((sid, data))),
        controls=SimpleNamespace(snapshot=lambda sid, generation: None),
        settling={},
    )
    ref = SimpleNamespace(session_id=SID)
    authority = SimpleNamespace(db=db, epoch=epoch, steer_transfers={})
    captured = {**captured_model_result(), '_p0_execution_proof': real_runner_proof(epoch, row)}
    settlement = {}
    commit_and_publish(authority, live, ref, row, 'done', 'completed', captured, settlement)
    assert settlement['settled']['status'] == 'terminal'
    assert len(pending_obligations(db)) == 1
    assert len(published) == 1


def test_real_caller_without_recipient_retains_source_open(admission):
    db, epoch, row = admission
    published = []
    live = SimpleNamespace(
        event_stream=SimpleNamespace(lock=threading.RLock(),
                                     publish=lambda sid, data: published.append(data)),
        controls=SimpleNamespace(snapshot=lambda sid, generation: None),
        settling={},
    )
    ref = SimpleNamespace(session_id=SID)
    authority = SimpleNamespace(db=db, epoch=epoch, steer_transfers={})
    captured = {**captured_model_result(), '_p0_execution_proof': real_runner_proof(epoch, row)}
    commit_and_publish(authority, live, ref, row, 'done', 'completed', captured, {})
    # Recipient binding is deferred to FIRE. Unavailable CDP does not erase OPEN.
    assert len(pending_obligations(db)) == 1
    assert tuple(pending_obligations(db)[0]) == (
        row['admission_id'], epoch, row['generation'], 'OPEN')


def test_positive_stale_epoch_refused_without_obligation(admission):
    db, epoch, row = admission
    with pytest.raises(RuntimeStoreError, match='stale_epoch'):
        finish_result(db, epoch=epoch + 1, row=row, response='done',
                      outcome='completed', result=captured_model_result(),
                      trusted_execution=real_runner_proof(epoch, row),
                  )
    assert pending_obligations(db) == []


def test_p0_insert_abort_rolls_back_terminal_settlement(admission):
    db, epoch, row = admission
    db._conn.execute('''CREATE TABLE p0_completion_obligations (
        admission_id TEXT PRIMARY KEY, source_epoch INTEGER NOT NULL,
        source_generation INTEGER NOT NULL, state TEXT NOT NULL
    )''')
    db._conn.execute('''CREATE TRIGGER force_p0_abort BEFORE INSERT ON
        p0_completion_obligations BEGIN SELECT RAISE(ABORT, 'forced_p0_abort'); END''')
    db._conn.commit()
    with pytest.raises(sqlite3.DatabaseError, match='forced_p0_abort'):
        finish_result(
            db, epoch=epoch, row=row, response='done', outcome='completed',
            result=captured_model_result(), trusted_execution=real_runner_proof(epoch, row),
        )
    assert get_session_admission(db, admission_id=row['admission_id'])['status'] == 'started'
    assert pending_obligations(db) == []


def test_open_obligation_is_durable_after_independent_connection(admission):
    db, epoch, row = admission
    finish_result(
        db, epoch=epoch, row=row, response='done', outcome='completed',
        result=captured_model_result(), trusted_execution=real_runner_proof(epoch, row),
    )
    with sqlite3.connect(str(db.db_path)) as separate:
        record = separate.execute(
            'SELECT state, source_generation FROM p0_completion_obligations '
            'WHERE admission_id=?', (row['admission_id'],)).fetchone()
    assert record == ('OPEN', row['generation'])


def test_rotated_recipient_never_consulted_during_source_settlement(admission):
    db, epoch, row = admission
    published = []
    live = SimpleNamespace(
        event_stream=SimpleNamespace(lock=threading.RLock(),
                                     publish=lambda sid, data: published.append(data)),
        controls=SimpleNamespace(snapshot=lambda sid, generation: None),
        settling={},
    )
    ref = SimpleNamespace(session_id=SID)
    def forbidden_recipient_lookup(*_args):
        raise AssertionError('No recipient or CDP lookup permitted at settlement')
    authority = SimpleNamespace(
        db=db, epoch=epoch, steer_transfers={},
        resolve_p0_completion_target=forbidden_recipient_lookup,
    )
    captured = {**captured_model_result(), '_p0_execution_proof': real_runner_proof(epoch, row)}
    commit_and_publish(authority, live, ref, row, 'done', 'completed', captured, {})
    assert len(pending_obligations(db)) == 1
    assert tuple(pending_obligations(db)[0]) == (
        row['admission_id'], epoch, row['generation'], 'OPEN')
    # The only publish above is a local completion frame, not a host sender call.


def test_caller_cannot_pass_destination_at_settlement(admission):
    db, epoch, row = admission
    with pytest.raises(TypeError, match='p0_target'):
        finish_result(
            db, epoch=epoch, row=row, response='done', outcome='completed',
            result=captured_model_result(), trusted_execution=True,
            p0_target={'destination_role': ROLE, 'destination_route': 'forged-route',
                       'destination_generation': 17})
    assert pending_obligations(db) == []
    assert get_session_admission(db, admission_id=row['admission_id'])['status'] == 'started'


def test_source_open_schema_has_no_destination_columns(admission):
    db, epoch, row = admission
    finish_result(db, epoch=epoch, row=row, response='done', outcome='completed',
                  result=captured_model_result(),
                  trusted_execution=real_runner_proof(epoch, row))
    cols = [item[1] for item in db._conn.execute(
        'PRAGMA table_info(p0_completion_obligations)').fetchall()]
    assert cols == ['admission_id', 'source_epoch', 'source_generation', 'state']
    assert len(pending_obligations(db)) == 1


def test_incomplete_model_result_is_not_a_source_wake(admission):
    db, epoch, row = admission
    incomplete = captured_model_result()
    incomplete['result']['completed'] = False
    finish_result(db, epoch=epoch, row=row, response='done',
                  outcome='completed', result=incomplete,
                  trusted_execution=real_runner_proof(epoch, row))
    assert pending_obligations(db) == []
