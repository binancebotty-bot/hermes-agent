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
    config={'native_wake': {'enabled': True}}
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    assert wake_hooks.enrol_admission(admission_id='a', session_id='s', principal='p', target='t', opted_in=True, config=config)
    row = {'admission_id': 'a', 'request_id': 'a', 'target_session_id': 's', 'principal_id': 'p', 'status': 'completed'}
    assert wake_hooks.completed(row, outcome='completed', config=config)
    assert wake_hooks.completed(row, outcome='completed', config=config) is False
    conn = sqlite3.connect(tmp_path / 'wake_outbox.sqlite3')
    assert conn.execute('select count(*) from outbox').fetchone()[0] == 1
    assert wake_hooks.deliver('a', config=config)[0] == 'HELD'


def _count_rows(path):
    if not path.exists():
        return 0
    with sqlite3.connect(path) as conn:
        return conn.execute('select count(*) from outbox').fetchone()[0]


def test_cron_session_is_not_enrolled(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    assert wake_hooks.enrol_admission(admission_id='cron', session_id='s', principal='p', target='t', opted_in=True, cron=True, config={'native_wake': {'enabled': True}}) is False
    assert _count_rows(tmp_path / 'wake_outbox.sqlite3') == 0


def test_nested_session_is_not_enrolled(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    assert wake_hooks.enrol_admission(admission_id='nested', session_id='s', principal='p', target='t', opted_in=True, nested=True, config={'native_wake': {'enabled': True}}) is False
    assert _count_rows(tmp_path / 'wake_outbox.sqlite3') == 0


def test_interrupted_session_is_not_enrolled(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    assert wake_hooks.enrol_admission(admission_id='interrupted', session_id='s', principal='p', target='t', opted_in=True, interrupted=True, config={'native_wake': {'enabled': True}}) is False
    assert _count_rows(tmp_path / 'wake_outbox.sqlite3') == 0


def test_unregistered_session_is_not_enrolled(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    assert wake_hooks.enrol_admission(admission_id='unregistered', session_id='s', principal='p', target='t', opted_in=True, registered=False, config={'native_wake': {'enabled': True}}) is False
    assert _count_rows(tmp_path / 'wake_outbox.sqlite3') == 0


def test_double_settlement_and_delivery_send_at_most_once(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    config = {'native_wake': {'enabled': True}}
    wake_hooks.enrol_admission(admission_id='twice', session_id='s', principal='p', target='t', opted_in=True, config=config)
    row = {'admission_id': 'twice', 'target_session_id': 's'}
    wake_hooks.completed(row, outcome='completed', config=config)
    wake_hooks.completed(row, outcome='completed', config=config)
    class Adapter:
        calls = 0
        def send(self, envelope):
            self.calls += 1
            return ('BUSY', '')
    adapter = Adapter()
    assert wake_hooks.deliver('twice', adapter, config)[0] == 'HELD'
    assert wake_hooks.deliver('twice', adapter, config)[0] == 'HELD'
    assert adapter.calls == 1
    assert _count_rows(tmp_path / 'wake_outbox.sqlite3') == 1


def test_reconcile_recovers_consumed_admission_once(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    config = {'native_wake': {'enabled': True}}
    wake_hooks.enrol_admission(admission_id='crash', session_id='s', principal='p', target='t', opted_in=True, config=config)
    db = tmp_path / 'wake_outbox.sqlite3'
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE enrolments SET state='consumed' WHERE task_key='crash'")
    rows = [{'admission_id': 'crash', 'target_session_id': 's', 'outcome': 'completed'}]
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

