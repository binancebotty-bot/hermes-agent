"""Exact execution results commit with settlement, before delivery/publication."""
from contextvars import ContextVar

from hermes_state_runtime import RuntimeStoreError, _admission, _epoch, settle_session_input

execution_result: ContextVar[dict | None] = ContextVar("execution_result", default=None)
from hermes_state_terminal import RESULT_PREFIX as _RESULT_PREFIX, load_result


# A caller-supplied Boolean is not execution evidence. This private capability is
# installed only after TurnRunner.run_sync returned from the real in-process agent.
_P0_EXECUTED = object()
_P0_PROOF_SEAL = object()


class _P0ExecutionProof:
    __slots__ = ('admission_id', 'epoch', 'generation', '_seal')

    def __init__(self, admission_id, epoch, generation):
        self.admission_id, self.epoch, self.generation = admission_id, epoch, generation
        self._seal = _P0_PROOF_SEAL


def mark_in_process_execution():
    """Called by the real TurnRunner execution branch, never from a model result."""
    captured = execution_result.get()
    if captured is not None:
        captured['_p0_executed'] = _P0_EXECUTED


def admitted_execution_proof(captured, *, epoch, row):
    """Bind the private runner witness to the claim that actually ran."""
    if not isinstance(captured, dict) or captured.get('_p0_executed') is not _P0_EXECUTED:
        return None
    return _P0ExecutionProof(row['admission_id'], epoch, row['generation'])



def record_unexecuted_failure(reply, reason=None, error=None):
    """The admitted turn never ran because it failed (agent initialization raised, history
    unreadable): commit a failed result for the executing admission, so no surface settles it
    ``completed`` with the apology as the turn's output. A *reason* is stamped as the turn's
    non-retryable failure code, so clients render that code's card. *error* replaces the reply
    as the result's ``error`` (the raw cause a local client shows as its details line).
    Returns ``reply`` unchanged."""
    captured = execution_result.get()
    if captured is not None and 'result' not in captured:
        captured['result'] = {'final_response': '', 'messages': [], 'failed': True, 'completed': False,
                              'error': str(error or reply or 'The admitted turn failed.')}
        if reason:
            from agent.turn_failure_copy import stamp_failure
            stamp_failure(captured['result'], reason, False)
    return reply


def record_terminal_failure(result):
    """Retain an owner-side failure even if an abandoned executor returns while delivery unwinds."""
    captured = execution_result.get()
    if captured is not None:
        captured['terminal_result'] = result
    return result


def _redacted(value):
    """The stored result is a state.db copy of the model's answer and history: redacted at this
    storage boundary like every transcript row (``security.redact_secrets``). The live viewer
    prints the completion event, never this copy."""
    if isinstance(value, str):
        from agent.redact import redact_sensitive_text
        return redact_sensitive_text(value)
    if isinstance(value, dict):
        return {k: _redacted(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redacted(v) for v in value]
    return value


def retain_result(db, *, epoch, row, result):
    return settle_session_input(db, epoch=epoch, admission_id=row['admission_id'],
                                generation=row['generation'], outcome='completed', result=_redacted(result))


def prepare_result(row, response, outcome, result=None):
    """``(outcome, stored_result)``: the outcome the result's own flags imply and the compact,
    redacted copy settlement stores. Pure CPU work, safe off-loop and outside the stream lock."""
    if result is None:
        result = {'result': {'final_response': response or '', 'messages': []}, 'usage': {}}
    value = result['result']
    if value.get('interrupted'):
        outcome = 'interrupted'
    elif value.get('failed') or value.get('error'):
        outcome = 'failed'
    if outcome in ('failed', 'interrupted'):
        value['failed' if outcome == 'failed' else 'interrupted'] = True
        value['completed'] = False
    # Compact before redacting, so the walk covers this turn's output, not every earlier turn.
    from hermes_state_terminal import compact_result
    return outcome, _redacted(compact_result(result, user_message=row['payload'].get('text')))


def finish_result(db, *, epoch, row, response, outcome, result=None, prepared=None, transfers=None,
                  trusted_execution=False):
    """Delivery failure cannot rewrite an already committed execution outcome.

    `result` is the exact structured result captured in-process; the managed
    worker path commits its own before this runs and is read back here.
    `prepared` is ``prepare_result``'s output computed before the caller took a lock.
    `transfers` are the queued steers handed to this claim; they settle in the same write.
    `trusted_execution` must be an execution-bound private capability minted only
    after the real TurnRunner runs; a caller's True flag grants nothing.
    Target role, route and generation are intentionally absent at settlement;
    a separate delivery worker resolves the current recipient when FIRE is authorised.
    """
    with db._read_ctx() as conn:
        _epoch(conn, epoch)
        current = _admission(conn, row['admission_id'])
        if current['owner_epoch'] != epoch or current['generation'] != row['generation']:
            raise RuntimeStoreError('stale_generation')
        if current['status'] == 'terminal':
            saved = conn.execute('SELECT value FROM state_meta WHERE key=?',
                                 (_RESULT_PREFIX + row['admission_id'],)).fetchone()
            if saved is None:
                raise RuntimeStoreError('storage_unavailable')
            result = load_result(saved[0])
            return dict(current), result['result'].get('final_response') or ''
    outcome, stored = prepared or prepare_result(row, response, outcome, result)
    # A turn the owner's restart/stop interrupted: its reason commits as a Hermes transcript notice
    # in this same write, so a viewer that reconnects after the restart reads why (#134889 GW-21);
    # the live ``message.complete.warning`` carries the same copy. A retry that finds the row
    # terminal returns above, so the notice is written once.
    # A gateway-owned refusal (a message typed into a cron session) commits its explanation and
    # error card as the transcript's failed-turn boundary in this same write too: the admission
    # result is not history, so a reopened chat would otherwise show nothing about the refusal.
    from gateway.session_settlement_recovery import gateway_refusal_boundary, owner_stop_notice
    notice, display_metadata = owner_stop_notice(result, outcome, row), None
    if notice is None and (refusal := gateway_refusal_boundary(result, outcome)) is not None:
        notice, display_metadata = refusal
    write = None if notice is None else (lambda conn, _current: close_discarded_turn(
        db, conn, row, notice, replace_after=row.get('transcript_mark'), display_metadata=display_metadata))
    # The obligation and terminal admission must survive (or roll back) together.
    # No caller-supplied destination may enter this durable source-only row.
    p0_eligible = (
        outcome == 'completed' and isinstance(stored, dict)
        and isinstance(stored.get('result'), dict)
        and stored['result'].get('completed') is True
        and type(trusted_execution) is _P0ExecutionProof
        and trusted_execution._seal is _P0_PROOF_SEAL
        and trusted_execution.admission_id == row['admission_id']
        and trusted_execution.epoch == epoch
        and trusted_execution.generation == row['generation']
    )
    if p0_eligible:
        notice_write = write
        def write(conn, current):
            if notice_write is not None:
                notice_write(conn, current)
            conn.execute('''CREATE TABLE IF NOT EXISTS p0_completion_obligations (
                admission_id TEXT PRIMARY KEY,
                source_epoch INTEGER NOT NULL,
                source_generation INTEGER NOT NULL,
                state TEXT NOT NULL
            )''')
            conn.execute('''INSERT INTO p0_completion_obligations
                (admission_id, source_epoch, source_generation, state)
                VALUES (?, ?, ?, 'OPEN')''',
                (current['admission_id'], epoch, current['generation']))
    settled = settle_session_input(db, epoch=epoch, admission_id=row['admission_id'],
        generation=row['generation'], outcome=outcome, result=stored, _transfers=transfers,
        _terminal_write=write)
    return settled, response


def admission_result(db, admission_id):
    with db._read_ctx() as conn:
        row = _admission(conn, admission_id)
        if row['status'] != 'terminal':
            return None
        saved = conn.execute('SELECT value FROM state_meta WHERE key=?',
                             (_RESULT_PREFIX + admission_id,)).fetchone()
        return load_result(saved[0]) if saved else None


def close_discarded_turn(db, conn, row, notice=None, *, replace_after=None, display_metadata=None):
    """Close a discarded ``unknown`` turn the way a failed turn is closed, on the resolution's
    own transaction (``resolve_unknown_session_input(_terminal_write=...)``).

    The lost input stays in the transcript for the user to resend, but an open ``user`` tail would be
    merged into the follower's provider request by consecutive-user repair, re-sending the discarded
    turn as context. A Hermes-authored boundary (``display_kind=failed_turn``, stripped of its type
    before the wire) ends it. Its side effects are unknown, so the hedged copy. The boundary lands on
    the CURRENT physical transcript (local reset/compression lineage tip), resolved on this
    connection. Idempotent on the durable tail, like the gateway and core failed-turn closers.

    A turn lost while parked on a tool (an approval or clarify prompt) ends on its assistant
    ``tool_calls`` row with no result. That tail is open too: repair prunes the unanswered call,
    drops the now-empty row and merges the discarded input into the follower's user turn, so it
    gets the same boundary (merged onto the call row, whose calls are then pruned).

    ``notice`` (an unattended automation turn settled by the owner, GW-10) replaces the hedged copy
    and is written even on a closed tail: nobody resolved it, so the transcript must say why.

    ``replace_after`` (the claim's transcript mark, an owner-stop notice): when the tail is a
    failed-turn boundary written after that mark, it is THIS turn's own (the agent's core closer
    wrote "Send it again" for the interrupted turn), so the notice replaces its copy instead of
    stacking a second boundary (#134889 GW-21). An earlier turn's boundary stays.

    ``display_metadata`` (``{error, error_surface}``) rides the boundary so a reopened chat redraws
    the turn's error card from history (``agent.conversation_loop._failed_turn_display_metadata``)."""
    import time
    from agent.turn_failure_copy import FAILED_TURN_DISPLAY_KIND, PARTIAL_FAILED_TURN_NOTICE
    from hermes_state_local_lineage import local_physical_target
    from hermes_state_runtime import _canonical_chain
    target = _canonical_chain(conn, local_physical_target(conn, row['target_session_id']))[-1]
    tail = conn.execute("SELECT role, tool_calls, id, display_kind FROM messages WHERE session_id=? AND active=1 "
                        "AND role NOT IN ('session_meta','system') ORDER BY id DESC LIMIT 1", (target,)).fetchone()
    if (notice is not None and type(replace_after) is int and tail is not None and tail[0] == 'assistant'
            and tail[3] == FAILED_TURN_DISPLAY_KIND and tail[2] > replace_after):
        conn.execute("UPDATE messages SET content=?, display_metadata=? WHERE id=?",
                     (notice, db._encode_display_metadata(display_metadata), tail[2]))
        return True
    if notice is None and (tail is None or not (
            tail[0] == 'user' or (tail[0] == 'assistant' and tail[1] not in (None, '', '[]')))):
        return False
    boundary = {'role': 'assistant', 'content': notice or PARTIAL_FAILED_TURN_NOTICE, 'timestamp': time.time(),
                'display_kind': FAILED_TURN_DISPLAY_KIND}
    if display_metadata:
        boundary['display_metadata'] = display_metadata
    db._append_messages_in_transaction(conn, target, [boundary])
    return True
