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
