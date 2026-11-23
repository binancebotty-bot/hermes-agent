"""Hermes-native wake hooks. No browser or transport imports."""
import datetime
import os
import sys
from pathlib import Path
from gateway._wake_enrol import wake_enrol as _store
sys.modules.setdefault('wake_enrol', _store)
from gateway._wake_enrol import wake_deliver as _delivery


def _config(config=None):
    if config is not None:
        return config
    try:
        from gateway.config import load_gateway_config
        return load_gateway_config().to_dict()
    except Exception:
        return {}


def enabled(config=None):
    return bool(_config(config).get('native_wake', {}).get('enabled', False))


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _connect():
    root = Path(os.environ.get('HERMES_HOME') or (Path.home() / '.hermes'))
    root.mkdir(parents=True, exist_ok=True)
    return _delivery.connect(str(root / 'wake_outbox.sqlite3'))


def enrol_admission(*, admission_id, session_id, principal, opted_in=False, cron=False, nested=False, interrupted=False, registered=True, config=None, target=None):
    settings = _config(config).get('native_wake', {})
    recipient = settings.get('recipient_conversation_id', '')
    if not enabled(config) or not opted_in or cron or nested or interrupted or not registered or not all((admission_id, session_id, principal, recipient)):
        return False
    conn = _connect()
    try:
        _delivery.ensure_schema(conn)
        from hashlib import sha256
        digest = sha256((admission_id + '\0' + session_id).encode()).hexdigest()
        binding = _store.resolve_operation(conn, admission_id, principal, session_id, digest, recipient, 600, _now())
        if binding[0] == _store.CREATED:
            return _store.link_admission(conn, admission_id, admission_id) == _store.LINKED
        if binding[0] == _store.REPLAY:
            _store.link_admission(conn, admission_id, admission_id)
        return False
    finally:
        conn.close()


def completed(row, *, outcome, config=None):
    if not enabled(config) or outcome != 'completed' or not row.get('admission_id') or not row.get('session_id') or not row.get('principal_id'):
        return False
    conn = _connect()
    try:
        enrolled = conn.execute('SELECT task_key,target,session_id,principal,admission_id,state FROM enrolments WHERE admission_id=?', (row['admission_id'],)).fetchone()
        settings = _config(config).get('native_wake', {})
        if enrolled is None or enrolled[1] != settings.get('recipient_conversation_id') or enrolled[2] != row['session_id'] or enrolled[3] != row['principal_id'] or enrolled[4] != row['admission_id'] or enrolled[5] != 'open':
            return False
        result = _store.resolve_and_enqueue(conn, {'admission_id': row['admission_id'], 'request_id': enrolled[0], 'session_id': row['session_id'], 'outcome': outcome}, _now())
        return result == _store.ENROLLED
    finally:
        conn.close()


def reconcile(rows, config=None):
    if not enabled(config):
        return 0
    conn = _connect()
    try:
        values = []
        for row in rows:
            linked = conn.execute('SELECT task_key,session_id,principal,target FROM enrolments WHERE admission_id=? AND state=?', (row['admission_id'], 'consumed')).fetchone()
            session = row.get('session_id', row.get('target_session_id'))
            recipient = _config(config).get('native_wake', {}).get('recipient_conversation_id')
            if linked and linked[1] == session and linked[2] == row.get('principal_id') and linked[3] == recipient:
                values.append({'admission_id': row['admission_id'], 'request_id': linked[0], 'session_id': session, 'outcome': row.get('outcome'), 'created_at': _now()})
        return _store.reconcile(conn, values)
    finally:
        conn.close()


def reconcile_startup(authority, config=None):
    if not enabled(config):
        return 0
    with authority.db._read_ctx() as conn:
        rows = [dict(row) for row in conn.execute("SELECT admission_id,target_session_id,principal_id,outcome FROM session_admissions WHERE status='terminal' AND outcome='completed'")]
    return reconcile(rows, config)


def deliver(admission_id, adapter=None, config=None):
    if not enabled(config):
        return 'DISABLED'
    settings = _config(config).get('native_wake', {})
    recipient = settings.get('recipient_conversation_id', '')
    # No approved transport is configured; an empty binding must never reach deliver_once.
    if not recipient or adapter is None:
        return 'HELD_NO_TARGET'
    return 'HELD_NO_TARGET'


class _FailClosedAdapter:
    def send(self, envelope):
        return (_delivery.BUSY, '')
