"""Host-free process-death test: COMMIT -> hard exit -> reopen same SQLite file."""
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CrashAfterCommit(unittest.TestCase):
    def test_committed_open_obligation_survives_process_death_and_retirement(self):
        script = '''
import os, sqlite3, sys
from gateway.p0_completion_obligation import install_schema, enqueue_first_completion
conn=sqlite3.connect(sys.argv[1], isolation_level=None)
conn.row_factory=sqlite3.Row
conn.execute('PRAGMA foreign_keys=ON')
conn.executescript("""CREATE TABLE sessions(id TEXT PRIMARY KEY);
CREATE TABLE session_admissions(admission_id TEXT PRIMARY KEY,
 target_session_id TEXT REFERENCES sessions(id) ON DELETE RESTRICT,
 status TEXT, outcome TEXT, owner_epoch INTEGER, generation INTEGER);
INSERT INTO sessions VALUES('s7');
INSERT INTO session_admissions VALUES('a7','s7','started',NULL,8,12);""")
install_schema(conn)
conn.execute('BEGIN IMMEDIATE')
enqueue_first_completion(conn, admission_id='a7', event_kind='execution-completion',
 source_epoch=8, source_generation=12, authentic_source='managed-finished-result',
 result_digest='f'*64, outcome='completed', destination_role='gpt6:external-monitor',
 destination_route='verified-target', destination_generation=11,
 msg_id='M7', nonce='N7', created_at=555)
conn.execute("UPDATE session_admissions SET status='terminal',outcome='completed' WHERE admission_id='a7'")
conn.commit()
os._exit(42)  # deliberate hard process death BEFORE any postcommit sender hint
'''
        with tempfile.TemporaryDirectory() as folder:
            db = str(Path(folder) / 'p0.db')
            completed = subprocess.run([sys.executable, '-c', script, db], capture_output=True, text=True)
            self.assertEqual((completed.returncode, completed.stdout, completed.stderr), (42, '', ''))
            for foreign_keys in ('ON', 'OFF'):
                conn = sqlite3.connect(db, isolation_level=None)
                conn.row_factory = sqlite3.Row
                conn.execute(f'PRAGMA foreign_keys={foreign_keys}')
                rows = conn.execute("SELECT state,event_json,event_digest,nonce FROM p0_completion_obligations WHERE admission_id='a7'").fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['state'], 'OPEN')
                self.assertEqual(len(rows[0]['event_digest']), 64)
                self.assertEqual(rows[0]['nonce'], 'N7')
                if foreign_keys == 'ON':
                    pinned = tuple(rows[0][field] for field in ('event_json','event_digest','nonce'))
                    conn.execute("DELETE FROM session_admissions WHERE admission_id='a7'")
                    conn.execute("DELETE FROM sessions WHERE id='s7'")
                else:
                    self.assertEqual(tuple(rows[0][field] for field in ('event_json','event_digest','nonce')), pinned)
                conn.close()


if __name__ == '__main__':
    unittest.main()
