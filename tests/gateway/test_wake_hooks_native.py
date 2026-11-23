import datetime
import os
import sqlite3

from gateway import wake_hooks


def test_default_disabled_does_not_create_outbox(tmp_path, monkeypatch):
    config={'native_wake': {'enabled': False}}
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    assert wake_hooks.enabled(config) is False
    assert wake_hooks.enrol_admission(admission_id='a', session_id='s', principal='p', target='t', opted_in=True, config=config) is False
    assert wake_hooks.completed({'admission_id': 'a'}, outcome='completed', config=config) is False
    assert wake_hooks.deliver('a', config=config) == 'DISABLED'
    assert not (tmp_path / 'wake_outbox.sqlite3').exists()


def test_enrol_then_completed_enqueues_once_and_fail_closed_delivery(tmp_path, monkeypatch):
    config={'native_wake': {'enabled': True, 'recipient_conversation_id': 'chat-id'}}
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    assert wake_hooks.enrol_admission(admission_id='a', session_id='s', principal='p', target='t', opted_in=True, config=config)
    row = {'admission_id': 'a', 'session_id': 's', 'principal_id': 'p', 'outcome': 'completed'}
    assert wake_hooks.completed(row, outcome='completed', config=config)
    assert wake_hooks.completed(row, outcome='completed', config=config) is False
    conn = sqlite3.connect(tmp_path / 'wake_outbox.sqlite3')
    assert conn.execute('select count(*) from outbox').fetchone()[0] == 1
    assert conn.execute('select principal,session_id,target from enrolments where admission_id=?', ('a',)).fetchone() == ('p', 's', 'chat-id')
    assert wake_hooks.deliver('a', config=config) == 'HELD_NO_TARGET'


def _count_rows(path):
    if not path.exists():
        return 0
    with sqlite3.connect(path) as conn:
        return conn.execute('select count(*) from outbox').fetchone()[0]


def test_cron_session_is_not_enrolled(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    assert wake_hooks.enrol_admission(admission_id='cron', session_id='s', principal='p', target='t', opted_in=True, cron=True, config={'native_wake': {'enabled': True, 'recipient_conversation_id': 'chat-id'}}) is False
    assert _count_rows(tmp_path / 'wake_outbox.sqlite3') == 0


def test_nested_session_is_not_enrolled(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    assert wake_hooks.enrol_admission(admission_id='nested', session_id='s', principal='p', target='t', opted_in=True, nested=True, config={'native_wake': {'enabled': True, 'recipient_conversation_id': 'chat-id'}}) is False
    assert _count_rows(tmp_path / 'wake_outbox.sqlite3') == 0


def test_interrupted_session_is_not_enrolled(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    assert wake_hooks.enrol_admission(admission_id='interrupted', session_id='s', principal='p', target='t', opted_in=True, interrupted=True, config={'native_wake': {'enabled': True, 'recipient_conversation_id': 'chat-id'}}) is False
    assert _count_rows(tmp_path / 'wake_outbox.sqlite3') == 0


def test_unregistered_session_is_not_enrolled(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    assert wake_hooks.enrol_admission(admission_id='unregistered', session_id='s', principal='p', target='t', opted_in=True, registered=False, config={'native_wake': {'enabled': True, 'recipient_conversation_id': 'chat-id'}}) is False
    assert _count_rows(tmp_path / 'wake_outbox.sqlite3') == 0


def test_double_settlement_and_delivery_send_at_most_once(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    config = {'native_wake': {'enabled': True, 'recipient_conversation_id': 'chat-id'}}
    wake_hooks.enrol_admission(admission_id='twice', session_id='s', principal='p', target='t', opted_in=True, config=config)
    row = {'admission_id': 'twice', 'session_id': 's', 'principal_id': 'p'}
    wake_hooks.completed(row, outcome='completed', config=config)
    wake_hooks.completed(row, outcome='completed', config=config)
    class Adapter:
        calls = 0
        def send(self, envelope):
            self.calls += 1
            return ('BUSY', '')
    adapter = Adapter()
    assert wake_hooks.deliver('twice', adapter, config) == 'HELD_NO_TARGET'
    assert wake_hooks.deliver('twice', adapter, config) == 'HELD_NO_TARGET'
    assert adapter.calls == 0
    assert _count_rows(tmp_path / 'wake_outbox.sqlite3') == 1


def test_reconcile_recovers_consumed_admission_once(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    config = {'native_wake': {'enabled': True, 'recipient_conversation_id': 'chat-id'}}
    wake_hooks.enrol_admission(admission_id='crash', session_id='s', principal='p', target='t', opted_in=True, config=config)
    db = tmp_path / 'wake_outbox.sqlite3'
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE enrolments SET state='consumed' WHERE admission_id='crash'")
    rows = [{'admission_id': 'crash', 'target_session_id': 's', 'principal_id': 'p', 'outcome': 'completed'}]
    assert wake_hooks.reconcile(rows, config) == 1
    assert wake_hooks.reconcile(rows, config) == 0
    assert _count_rows(db) == 1


def test_disabled_config_blocks_all_wake_paths(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    disabled = {'native_wake': {'enabled': False}}
    assert wake_hooks.enrol_admission(admission_id='off', session_id='s', principal='p', target='t', opted_in=True, config=disabled) is False
    assert wake_hooks.completed({'admission_id': 'off'}, outcome='completed', config=disabled) is False
    assert wake_hooks.reconcile([{'admission_id': 'off', 'outcome': 'completed'}], disabled) == 0
    assert wake_hooks.deliver('off', config=disabled) == 'DISABLED'
    assert _count_rows(tmp_path / 'wake_outbox.sqlite3') == 0


def test_configured_recipient_empty_refuses_enrolment(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    config = {'native_wake': {'enabled': True, 'recipient_conversation_id': ''}}
    assert wake_hooks.enrol_admission(admission_id='m', session_id='s', principal='p', opted_in=True, target='client-target', config=config) is False
    assert _count_rows(tmp_path / 'wake_outbox.sqlite3') == 0


def test_settled_session_or_principal_mismatch_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    config = {'native_wake': {'enabled': True, 'recipient_conversation_id': 'chat-id'}}
    wake_hooks.enrol_admission(admission_id='mismatch', session_id='s', principal='p', opted_in=True, config=config)
    assert wake_hooks.completed({'admission_id': 'mismatch', 'session_id': 'other', 'principal_id': 'p'}, outcome='completed', config=config) is False
    assert wake_hooks.completed({'admission_id': 'mismatch', 'session_id': 's', 'principal_id': 'other'}, outcome='completed', config=config) is False
    assert _count_rows(tmp_path / 'wake_outbox.sqlite3') == 0


def test_changed_config_recipient_refuses_completion(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    original = {'native_wake': {'enabled': True, 'recipient_conversation_id': 'chat-one'}}
    changed = {'native_wake': {'enabled': True, 'recipient_conversation_id': 'chat-two'}}
    wake_hooks.enrol_admission(admission_id='dest', session_id='s', principal='p', opted_in=True, config=original)
    assert wake_hooks.completed({'admission_id': 'dest', 'session_id': 's', 'principal_id': 'p'}, outcome='completed', config=changed) is False
    assert _count_rows(tmp_path / 'wake_outbox.sqlite3') == 0


def test_replay_and_duplicate_enrolment_are_noops(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    config = {'native_wake': {'enabled': True, 'recipient_conversation_id': 'chat-id'}}
    kwargs = dict(admission_id='replay', session_id='s', principal='p', opted_in=True, config=config)
    assert wake_hooks.enrol_admission(**kwargs) is True
    assert wake_hooks.enrol_admission(**kwargs) is False
    row = {'admission_id': 'replay', 'session_id': 's', 'principal_id': 'p'}
    assert wake_hooks.completed(row, outcome='completed', config=config) is True
    assert wake_hooks.completed(row, outcome='completed', config=config) is False
    assert _count_rows(tmp_path / 'wake_outbox.sqlite3') == 1


def test_empty_recipient_delivery_never_calls_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    class Adapter:
        calls = 0
        def send(self, envelope):
            self.calls += 1
            return ('OK', 'proof')
    adapter = Adapter()
    config = {'native_wake': {'enabled': True, 'recipient_conversation_id': ''}}
    assert wake_hooks.deliver('missing', adapter, config) == 'HELD_NO_TARGET'
    assert adapter.calls == 0


def test_startup_reconciliation_uses_authority_db_and_is_idempotent(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    config = {'native_wake': {'enabled': True, 'recipient_conversation_id': 'chat-id'}}
    wake_hooks.enrol_admission(admission_id='boot', session_id='s', principal='p', opted_in=True, config=config)
    with sqlite3.connect(tmp_path / 'wake_outbox.sqlite3') as conn:
        conn.execute("UPDATE enrolments SET state='consumed' WHERE admission_id='boot'")
    authority_db = tmp_path / 'authority.sqlite3'
    with sqlite3.connect(authority_db) as conn:
        conn.execute('CREATE TABLE session_admissions (admission_id TEXT, target_session_id TEXT, principal_id TEXT, outcome TEXT, status TEXT)')
        conn.execute("INSERT INTO session_admissions VALUES ('boot','s','p','completed','terminal')")
    authority = SimpleNamespace(db=SimpleNamespace(db_path=str(authority_db)))
    assert wake_hooks.reconcile_startup(authority, config) == 1
    assert wake_hooks.reconcile_startup(authority, config) == 0
    assert _count_rows(tmp_path / 'wake_outbox.sqlite3') == 1


def test_startup_recovers_terminal_completion_with_open_enrolment(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    config = {'native_wake': {'enabled': True, 'recipient_conversation_id': 'chat-id'}}
    wake_hooks.enrol_admission(admission_id='open-gap', session_id='s', principal='p', opted_in=True, config=config)
    authority_db = tmp_path / 'authority.sqlite3'
    with sqlite3.connect(authority_db) as conn:
        conn.execute('CREATE TABLE session_admissions (admission_id TEXT, target_session_id TEXT, principal_id TEXT, outcome TEXT, status TEXT)')
        conn.execute("INSERT INTO session_admissions VALUES ('open-gap','s','p','completed','terminal')")
    authority = SimpleNamespace(db=SimpleNamespace(db_path=str(authority_db)))
    assert wake_hooks.reconcile_startup(authority, config) == 1
    assert wake_hooks.reconcile_startup(authority, config) == 0
    with sqlite3.connect(tmp_path / 'wake_outbox.sqlite3') as conn:
        assert conn.execute("SELECT count(*) FROM outbox WHERE admission_id='open-gap'").fetchone()[0] == 1
