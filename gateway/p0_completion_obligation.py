"""Isolated SQLite P0 obligation/ACK contract. NOT wired to a Hermes dispatcher.

Source provenance and recipient authentication MUST be verified by the eventual
trusted terminal-writer/ACK caller. A string tag is not independent evidence.
No external sends, live DB writes, migrations, or wake dispatch occur here.
"""
import hashlib
import json
import re
import sqlite3

_ALLOWED_SOURCES = frozenset({
    'managed-finished-result', 'inprocess-executed-result', 'captured-unknown-authentic',
})
_ALLOWED_OUTCOMES = frozenset({'completed', 'failed', 'interrupted', 'rejected'})


def install_schema(conn: sqlite3.Connection) -> None:
    """Host-free candidate table, not an authorised production migration."""
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS p0_completion_obligations (
        admission_id TEXT NOT NULL,
        event_kind TEXT NOT NULL CHECK(event_kind='execution-completion'),
        source_epoch INTEGER NOT NULL CHECK(source_epoch>0),
        source_generation INTEGER NOT NULL CHECK(source_generation>=0),
        provenance_source TEXT NOT NULL,
        outcome TEXT NOT NULL,
        result_digest TEXT NOT NULL,
        destination_role TEXT NOT NULL,
        destination_route TEXT NOT NULL,
        destination_generation INTEGER NOT NULL,
        msg_id TEXT NOT NULL UNIQUE,
        nonce TEXT NOT NULL UNIQUE,
        event_json TEXT NOT NULL,
        event_digest TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'OPEN'
            CHECK(state IN ('OPEN','QUEUED','UNCERTAIN','ACKED','CANCELLED_ERASURE')),
        created_at INTEGER NOT NULL,
        ack_at INTEGER,
        PRIMARY KEY(admission_id,event_kind)
    );
    CREATE INDEX IF NOT EXISTS idx_p0_completion_pending
        ON p0_completion_obligations(state, created_at);
    ''')


def enqueue_first_completion(conn: sqlite3.Connection, *, admission_id: str, event_kind: str,
                             source_epoch: int, source_generation: int, authentic_source: str,
                             result_digest: str, outcome: str, destination_role: str,
                             destination_route: str, destination_generation: int,
                             msg_id: str, nonce: str, created_at: int) -> bool:
    """Insert inside caller's existing BEGIN IMMEDIATE *before* terminal status UPDATE.

    Returns True for a new obligation, False only for an IDENTICAL retry. The
    caller must source-verify and authenticate the result provenance; this
    isolated storage primitive alone is NOT an eligibility proof.
    """
    if not conn.in_transaction:
        raise RuntimeError('same_transaction_required')
    if (event_kind != 'execution-completion' or authentic_source not in _ALLOWED_SOURCES
            or outcome not in _ALLOWED_OUTCOMES or not isinstance(result_digest, str)
            or re.fullmatch(r'[0-9a-f]{64}', result_digest) is None):
        raise ValueError('unverified_result_provenance')
    if any(not isinstance(v, str) or not v for v in
           (admission_id, destination_role, destination_route, msg_id, nonce)):
        raise ValueError('invalid_route_or_correlation')
    if any(type(v) is not int for v in (source_epoch, source_generation, destination_generation, created_at)):
        raise ValueError('invalid_generation_or_timestamp')
    row = conn.execute('''SELECT status, owner_epoch, generation FROM session_admissions
                          WHERE admission_id=?''', (admission_id,)).fetchone()
    if (row is None or row['status'] != 'started'
            or row['owner_epoch'] != source_epoch or row['generation'] != source_generation):
        raise ValueError('stale_or_missing_admission')
    payload = dict(admission_id=admission_id, event_kind=event_kind,
                   outcome=outcome, source_epoch=source_epoch,
                   source_generation=source_generation, provenance_source=authentic_source,
                   result_digest=result_digest, destination_role=destination_role,
                   destination_route=destination_route, destination_generation=destination_generation,
                   msg_id=msg_id, nonce=nonce)
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
    digest = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
    values = (admission_id, event_kind, source_epoch, source_generation, authentic_source,
              outcome, result_digest, destination_role, destination_route,
              destination_generation, msg_id, nonce, encoded, digest, created_at)
    insert = conn.execute('''INSERT INTO p0_completion_obligations
        (admission_id,event_kind,source_epoch,source_generation,provenance_source,
         outcome,result_digest,destination_role,destination_route,destination_generation,
         msg_id,nonce,event_json,event_digest,created_at)
         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
         ON CONFLICT(admission_id,event_kind) DO NOTHING''', values)
    if insert.rowcount:
        return True
    existing = conn.execute('''SELECT source_epoch,source_generation,provenance_source,
         outcome,result_digest,destination_role,destination_route,destination_generation,
         msg_id,nonce,event_json,event_digest,created_at
         FROM p0_completion_obligations WHERE admission_id=? AND event_kind=?''',
        (admission_id, event_kind)).fetchone()
    if tuple(existing) != values[2:]:
        raise ValueError('immutable_obligation_conflict')
    return False


def mark_transport_result(conn: sqlite3.Connection, *, admission_id: str,
                          event_kind: str, msg_id: str, transport: str) -> bool:
    """202=QUEUED, ambiguous=UNCERTAIN; NEITHER is recipient ACK."""
    state = {'queued': 'QUEUED', 'uncertain': 'UNCERTAIN'}.get(transport)
    if state is None:
        raise ValueError('invalid_transport_result')
    return bool(conn.execute('''UPDATE p0_completion_obligations SET state=?
        WHERE admission_id=? AND event_kind=? AND msg_id=? AND state='OPEN' ''',
        (state, admission_id, event_kind, msg_id)).rowcount)


def acknowledge_completion(conn: sqlite3.Connection, *, admission_id: str,
                           event_kind: str, source_epoch: int, source_generation: int,
                           destination_role: str, destination_route: str,
                           destination_generation: int, msg_id: str, nonce: str,
                           ack_at: int) -> bool:
    """Atomic correlated closure. Caller MUST separately authenticate recipient.

    Correlation alone is not proof of the remote caller's identity.
    """
    return bool(conn.execute('''UPDATE p0_completion_obligations
        SET state='ACKED', ack_at=? WHERE admission_id=? AND event_kind=?
        AND source_epoch=? AND source_generation=? AND destination_role=?
        AND destination_route=? AND destination_generation=?
        AND msg_id=? AND nonce=? AND state IN ('OPEN','QUEUED','UNCERTAIN')''',
        (ack_at, admission_id, event_kind, source_epoch, source_generation,
         destination_role, destination_route, destination_generation,
         msg_id, nonce)).rowcount)
