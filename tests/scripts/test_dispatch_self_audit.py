import json
from pathlib import Path
import sys
import tempfile
import unittest

from scripts.dispatch_self_audit import prove_claim, record_unrouteable


class SelfAuditTests(unittest.TestCase):
    def test_unrouted_envelopes_are_loud_and_counted(self):
        with tempfile.TemporaryDirectory() as temp:
            health = Path(temp) / 'dispatch_health.json'
            health.write_text(json.dumps({'other_health': 'preserved'}))
            output = []
            self.assertEqual(record_unrouteable(['z.md', 'a.md'], health, emit=output.append), 2)
            self.assertEqual(output, ['ALERT UNROUTEABLE 2 first=a.md'])
            self.assertEqual(json.loads(health.read_text()), {'other_health': 'preserved',
                'unrouteable_count': 2, 'unrouteable_first': 'a.md'})
            self.assertEqual(record_unrouteable([], health, emit=output.append), 0)
            self.assertEqual(output[-1], 'ALERT UNROUTEABLE 0 first=-')
            self.assertEqual(json.loads(health.read_text())['unrouteable_count'], 0)

    def test_done_requires_executed_successful_command_and_output(self):
        with self.assertRaises(ValueError):
            prove_claim('shipped', [])
        proof = prove_claim('example', [sys.executable, '-c', 'print("proof-ok")'])
        self.assertEqual(proof['exit_code'], 0)
        self.assertEqual(proof['stdout'], 'proof-ok\n')
        with self.assertRaises(RuntimeError):
            prove_claim('failing', [sys.executable, '-c', 'raise SystemExit(9)'])


if __name__ == '__main__':
    unittest.main()
