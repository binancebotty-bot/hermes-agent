"""RED-first regressions for independent source review F1/F2/F3 and replay clock."""
import sqlite3
import unittest

from gateway.p0_completion_obligation import install_schema, enqueue_first_completion


class IndependentReviewRegressions(unittest.TestCase):
    def setUp(self):
        # Intentionally the NORMAL sqlite3 connection with default tuple rows.
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.db.executescript('''
            CREATE TABLE sessions(id TEXT PRIMARY KEY);
            CREATE TABLE session_admissions(
                admission_id TEXT PRIMARY KEY, target_session_id TEXT REFERENCES sessions(id),
                status TEXT, outcome TEXT, owner_epoch INTEGER, generation INTEGER);
            INSERT INTO sessions VALUES('session');
            INSERT INTO session_admissions VALUES('admission','session','started',NULL,5,9);
        ''')
        install_schema(self.db)

    def tearDown(self):
        self.db.close()

    def enqueue(self, **changes):
        args = dict(admission_id='admission', event_kind='execution-completion',
                    source_epoch=5, source_generation=9,
                    authentic_source='managed-finished-result', result_digest='a' * 64,
                    outcome='completed', destination_role='gpt6:external-monitor',
                    destination_route='controller-verified-route', destination_generation=11,
                    msg_id='one', nonce='one-nonce', created_at=1000)
        args.update(changes)
        return enqueue_first_completion(self.db, **args)

    def test_f1_default_tuple_connection_can_enqueue_without_row_factory_side_effect(self):
        self.assertIsNone(self.db.row_factory)
        self.db.execute('BEGIN IMMEDIATE')
        self.assertTrue(self.enqueue())
        self.assertIsNone(self.db.row_factory)
        self.db.commit()

    def test_f2_install_schema_rejects_active_transaction_without_committing(self):
        self.db.execute('BEGIN IMMEDIATE')
        self.db.execute("INSERT INTO sessions VALUES ('should-rollback')")
        with self.assertRaisesRegex(RuntimeError, 'schema_install_in_transaction'):
            install_schema(self.db)
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertIsNone(self.db.execute("SELECT id FROM sessions WHERE id='should-rollback'").fetchone())

    def test_f3_postcommit_exact_replay_is_noop_even_when_clock_changes(self):
        self.db.execute('BEGIN IMMEDIATE')
        self.assertTrue(self.enqueue())
        self.db.execute("UPDATE session_admissions SET status='terminal',outcome='completed'")
        self.db.commit()
        original = tuple(self.db.execute('''SELECT event_json,event_digest,msg_id,nonce,created_at
                                            FROM p0_completion_obligations''').fetchone())
        self.db.execute('BEGIN IMMEDIATE')
        self.assertFalse(self.enqueue(created_at=9999))
        self.db.commit()
        self.assertEqual(original, tuple(self.db.execute('''SELECT event_json,event_digest,msg_id,nonce,created_at
                                                          FROM p0_completion_obligations''').fetchone()))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM p0_completion_obligations').fetchone()[0], 1)

    def test_f3_mismatch_after_terminal_rejected_without_new_send(self):
        self.db.execute('BEGIN IMMEDIATE')
        self.assertTrue(self.enqueue())
        self.db.execute("UPDATE session_admissions SET status='terminal',outcome='completed'")
        self.db.commit()
        self.db.execute('BEGIN IMMEDIATE')
        for changed in ({'nonce': 'wrong'}, {'result_digest': 'b'*64},
                        {'destination_generation': 12}, {'destination_route': 'other'},
                        {'source_generation': 10}):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'immutable_obligation_conflict'):
                self.enqueue(**changed)
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM p0_completion_obligations').fetchone()[0], 1)

    def test_f3_missing_row_cannot_create_unanchored_obligation(self):
        self.db.execute('DELETE FROM session_admissions')
        self.db.execute('BEGIN IMMEDIATE')
        with self.assertRaisesRegex(ValueError, 'stale_or_missing_admission'):
            self.enqueue()
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM p0_completion_obligations').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
