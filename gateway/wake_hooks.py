"""Hermes-native wake hooks. No browser or transport imports."""
import datetime
import os
import sys
from pathlib import Path
from gateway._wake_enrol import wake_enrol as _store
sys.modules.setdefault('wake_enrol', _store)
from gateway._wake_enrol import wake_deliver as _delivery


def enabled(config=None):
    if config is not None:
        return bool(config.get('native_wake', {}).get('enabled', False))
    try:
        from gateway.config import load_gateway_config
        return bool(load_gateway_config().to_dict().get('native_wake', {}).get('enabled', False))
    except Exception:
        return False


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _connect():
    root = Path(os.environ.get('HERMES_HOME') or (Path.home() / '.hermes'))
    root.mkdir(parents=True, exist_ok=True)
    return _delivery.connect(str(root / 'wake_outbox.sqlite3'))


def enrol_admission(*, admission_id, session_id, principal, target, opted_in=False, cron=False, nested=False, interrupted=False, registered=True, config=None):
    if not enabled(config) or not opted_in or cron or nested or interrupted or not registered or not all((admission_id, session_id, principal, target)):
        return False
    conn = _connect()
    try:
        result = _store.enrol(conn, admission_id, target, 600, _now())
        if result == _store.ENROLLED:
            conn.execute('UPDATE enrolments SET session_id=?, principal=?, admission_id=? WHERE task_key=?', (session_id, principal, admission_id, admission_id))
            conn.commit()
        return result in (_store.ENROLLED, _store.ALREADY_ENROLLED)
    finally:
        conn.close()


def completed(row, *, outcome, config=None):
    if not enabled(config) or outcome != 'completed':
        return False
    conn = _connect()
    try:
        return _store.resolve_and_enqueue(conn, {'admission_id': row.get('admission_id'), 'request_id': row.get('admission_id'), 'session_id': row.get('target_session_id'), 'outcome': outcome}, _now()) == _store.ENROLLED
    finally:
        conn.close()


def reconcile(rows, config=None):
    if not enabled(config):
        return 0
    conn = _connect()
    try:
        values = [{'admission_id': r['admission_id'], 'request_id': r['admission_id'], 'session_id': r.get('target_session_id'), 'outcome': r.get('outcome'), 'created_at': _now()} for r in rows]
        return _store.reconcile(conn, values)
    finally:
        conn.close()


def deliver(admission_id, adapter=None, config=None):
    if not enabled(config):
        return 'DISABLED'
    conn = _connect()
    try:
        return _delivery.deliver_once(conn, admission_id, adapter or _FailClosedAdapter(), '', '', True, _now())
    finally:
        conn.close()


class _FailClosedAdapter:
    def send(self, envelope):
        return (_delivery.BUSY, '')
