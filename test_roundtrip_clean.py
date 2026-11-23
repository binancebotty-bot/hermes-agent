#!/usr/bin/env python3

import os
import re
import sqlite3
import subprocess
import sys
import time

H = os.path.join(os.path.expanduser('~'), '.hermes')
KB = os.path.join(H, 'kanban.db')
ST = os.path.join(H, 'state.db')
LEDGER = os.path.join(H, 'state', 'undelivered_envelopes.jsonl')
EXE = os.path.join(H, 'hermes-agent', '.venv', 'Scripts', 'hermes.exe')
DEFAULT_ISSUER = 'test-issuer-' + str(int(time.time()))

def sh_clean(args):
    """Run hermes command with clean environment (no HERMES_KANBAN_TASK)"""
    env = dict(os.environ)
    # Remove HERMES_KANBAN_TASK to avoid the delegate_task child context error
    env.pop('HERMES_KANBAN_TASK', None)
    env['PAID_GUARD_ALLOW'] = '1'
    r = subprocess.run([EXE] + args, capture_output=True, text=True, errors='replace', env=env)
    return r.returncode, (r.stdout or '') + (r.stderr or '')

def main():
    issuer = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ISSUER
    results = []

    def check(name, ok, detail=''):
        results.append((name, ok, detail))
        print('%s %s %s' % ('PASS' if ok else 'FAIL', name, detail))

    print(f"Testing with issuer: {issuer}")
    
    # Hop 1: create with an issuer and prove the address is stored.
    print("\\n=== Hop 1: Create task ===")
    rc, out = sh_clean(['kanban', 'create', 'ROUNDTRIP PROOF - archive me', '--assignee', 'default',
                  '--priority', '0', '--provider', 'zai', '--model', 'glm-4.5-flash',
                  '--issuer', issuer, '--body', 'Acceptance probe. No work required.'])
    print(f"Create command output: {out}")
    tid = None
    for tok in re.findall(r't_[0-9a-f]{8}', out):
        tid = tok
        break
    if not tid:
        check('create', False, 'no task id in output: %s' % out.strip()[-160:])
        return 1
    print(f"Created task: {tid}")
    
    # Check that issuer is stored
    con = sqlite3.connect('file:' + KB + '?mode=ro', uri=True)
    row = con.execute('select issuer, created_by, issuer_state from tasks where id=?', (tid,)).fetchone()
    con.close()
    check('issuer-stored', bool(row and row[0] == issuer), 'row=%s' % (row,))

    # Hop 2: completion must not crash, and must exit 0.
    print("\\n=== Hop 2: Complete task ===")
    rc2, out2 = sh_clean(['kanban', 'complete', tid, '--summary', 'roundtrip proof'])
    print(f"Complete command output: {out2}")
    crashed = 'Traceback' in out2 or 'UnboundLocalError' in out2
    check('complete-rc0', rc2 == 0 and not crashed,
          'rc=%d %s' % (rc2, ('CRASH: ' + out2.strip().splitlines()[-1][:90]) if crashed else ''))
    if crashed:
        print('\\nRESULT: RED - completion is broken; no receipt can exist. Card left as %s.' % tid)
        return 2

    # Hop 3: status actually moved.
    print("\\n=== Hop 3: Check status ===")
    con = sqlite3.connect('file:' + KB + '?mode=ro', uri=True)
    st = con.execute('select status from tasks where id=?', (tid,)).fetchone()
    con.close()
    check('status-done', bool(st and st[0] == 'done'), 'status=%s' % (st,))

    # Hop 4: a receipt addressed to the issuer exists - ledger record or delivered message.
    print("\\n=== Hop 4: Check receipt ===")
    ledger_hit = 0
    if os.path.isfile(LEDGER):
        with open(LEDGER, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                if tid in line and ('ORIGIN-RECEIPT' in line or issuer in line):
                    ledger_hit += 1
    con = sqlite3.connect('file:' + ST + '?mode=ro', uri=True)
    msg_hit = con.execute(
        "select count(*) from messages where session_id=? and content like ?",
        (issuer, '%' + tid + '%')).fetchone()[0]
    con.close()
    check('receipt-written', ledger_hit > 0 or msg_hit > 0,
          'ledger=%d messages=%d' % (ledger_hit, msg_hit))

    ok = all(x[1] for x in results)
    print('\\nRESULT: %s (%d/%d hops) card=%s' % ('GREEN - round trip closed' if ok else 'RED', 
                                                sum(1 for x in results if x[1]), len(results), tid))

    print("\\n=== Test completed ===")
    return 0 if ok else 1

if __name__ == '__main__':
    sys.exit(main())