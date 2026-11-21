"""Host-free SQLite contract tests; NOT a Hermes end-to-end/integration test."""
import sqlite3
import unittest

from gateway.p0_completion_obligation import (
    install_schema, enqueue_first_completion, mark_transport_result,
    acknowledge_completion,
)

ADMISSION = 'adm-7'
ROLE = 'gpt6:external-monitor'
MSG = 'P0-WAKE-ADM7-1'
NONCE = 'P0-NONCE-ADM7-1'
DIGEST = 'a' * 64


class OutboxContract(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE sessions(id TEXT PRIMARY KEY);
            CREATE TABLE session_admissions(
                admission_id TEXT PRIMARY KEY,
                target_session_id TEXT REFERENCES sessions(id) ON DELETE RESTRICT,
                status TEXT NOT NULL, outcome TEXT,
                owner_epoch INTEGER NOT NULL, generation INTEGER NOT NULL);
            INSERT INTO sessions VALUES ('sess-7');
            INSERT INTO session_admissions VALUES ('adm-7','sess-7','started',NULL,5,9);
        ''')
        install_schema(self.db)

    def tearDown(self):
        self.db.close()

    def enqueue(self, *, authentic_source='managed-finished-result', source_epoch=5,
                source_generation=9, result_digest=DIGEST, msg_id=MSG, nonce=NONCE):
        return enqueue_first_completion(
            self.db, admission_id=ADMISSION, event_kind='execution-completion',
            source_epoch=source_epoch, source_generation=source_generation,
            authentic_source=authentic_source, result_digest=result_digest,
            outcome='completed', destination_role=ROLE,
            destination_route='controller-pane-verified-route', destination_generation=11,
            msg_id=msg_id, nonce=nonce, created_at=1000)

    def one(self):
        return self.db.execute('SELECT * FROM p0_completion_obligations').fetchone()

    def test_atomic_first_terminal_and_immutable_envelope(self):
        self.db.execute('BEGIN IMMEDIATE')
        self.assertTrue(self.enqueue())
        self.db.execute("UPDATE session_admissions SET status='terminal',outcome='completed' WHERE admission_id=?", (ADMISSION,))
        self.db.commit()
        row = self.one()
        self.assertEqual(row['state'], 'OPEN')
        self.assertEqual(row['destination_role'], ROLE)
        self.assertEqual(row['result_digest'], DIGEST)
        self.assertEqual(len(row['event_digest']), 64)
        self.assertNotIn('secret', row['event_json'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM p0_completion_obligations').fetchone()[0], 1)

    def test_failed_transaction_rolls_back_terminal_and_obligation(self):
        self.db.execute('BEGIN IMMEDIATE')
        self.enqueue()
        self.db.execute("UPDATE session_admissions SET status='terminal' WHERE admission_id=?", (ADMISSION,))
        self.db.rollback()
        self.assertIsNone(self.one())
        self.assertEqual(self.db.execute('SELECT status FROM session_admissions WHERE admission_id=?', (ADMISSION,)).fetchone()[0], 'started')

    def test_stale_generation_and_untrusted_source_fail_closed(self):
        self.db.execute('BEGIN IMMEDIATE')
        for kwargs in ({'source_epoch': 4}, {'source_generation': 10},
                       {'authentic_source': 'synthetic-prepare-result'},
                       {'authentic_source': 'pre-spawn-refusal'},
                       {'result_digest': ''}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.enqueue(**kwargs)
        self.assertIsNone(self.one())
        self.db.rollback()

    def test_duplicate_and_message_nonce_conflict(self):
        self.db.execute('BEGIN IMMEDIATE')
        self.assertTrue(self.enqueue())
        self.assertFalse(self.enqueue())
        with self.assertRaises(ValueError):
            self.enqueue(result_digest='b'*64)
        self.db.rollback()

    def test_202_or_uncertain_not_ack_and_exact_correlated_ack(self):
        self.db.execute('BEGIN IMMEDIATE')
        self.enqueue()
        self.db.execute("UPDATE session_admissions SET status='terminal' WHERE admission_id=?", (ADMISSION,))
        self.db.commit()
        self.assertTrue(mark_transport_result(self.db, admission_id=ADMISSION, event_kind='execution-completion', msg_id=MSG, transport='queued'))
        self.assertEqual(self.one()['state'], 'QUEUED')
        self.assertFalse(acknowledge_completion(self.db, admission_id=ADMISSION, event_kind='execution-completion', source_epoch=5, source_generation=9, destination_role='gpt6:other', destination_route='controller-pane-verified-route', destination_generation=11, msg_id=MSG, nonce=NONCE, ack_at=1010))
        self.assertFalse(acknowledge_completion(self.db, admission_id=ADMISSION, event_kind='execution-completion', source_epoch=5, source_generation=9, destination_role=ROLE, destination_route='controller-pane-verified-route', destination_generation=11, msg_id=MSG, nonce='WRONG', ack_at=1010))
        self.assertEqual(self.one()['state'], 'QUEUED')
        self.assertTrue(acknowledge_completion(self.db, admission_id=ADMISSION, event_kind='execution-completion', source_epoch=5, source_generation=9, destination_role=ROLE, destination_route='controller-pane-verified-route', destination_generation=11, msg_id=MSG, nonce=NONCE, ack_at=1010))
        self.assertFalse(acknowledge_completion(self.db, admission_id=ADMISSION, event_kind='execution-completion', source_epoch=5, source_generation=9, destination_role=ROLE, destination_route='controller-pane-verified-route', destination_generation=11, msg_id=MSG, nonce=NONCE, ack_at=1011))
        self.assertEqual(self.one()['state'], 'ACKED')

    def test_timeout_is_uncertain_not_delivered(self):
        self.db.execute('BEGIN IMMEDIATE')
        self.enqueue()
        self.db.commit()
        self.assertTrue(mark_transport_result(self.db, admission_id=ADMISSION, event_kind='execution-completion', msg_id=MSG, transport='uncertain'))
        self.assertEqual(self.one()['state'], 'UNCERTAIN')
        self.assertFalse(mark_transport_result(self.db, admission_id=ADMISSION, event_kind='execution-completion', msg_id=MSG, transport='queued'))

    def test_survives_retirement_and_source_result_prune(self):
        self.db.execute('BEGIN IMMEDIATE')
        self.enqueue()
        self.db.execute("UPDATE session_admissions SET status='terminal' WHERE admission_id=?", (ADMISSION,))
        self.db.commit()
        before = self.one()
        self.db.execute('DELETE FROM session_admissions WHERE admission_id=?', (ADMISSION,))
        self.db.execute('DELETE FROM sessions WHERE id=?', ('sess-7',))
        after = self.one()
        self.assertEqual((after['event_json'], after['event_digest'], after['nonce']),
                         (before['event_json'], before['event_digest'], before['nonce']))


if __name__ == '__main__':
    unittest.main()
