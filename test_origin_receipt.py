import tempfile
import os
from pathlib import Path

# Create temp dir
tmp = Path(tempfile.mkdtemp())
home = tmp / '.hermes'
home.mkdir()
os.environ['HERMES_HOME'] = str(home)

import sys
sys.path.insert(0, '/c/Users/wigmore/.hermes/hermes-agent')

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

kb.init_db()

with kbc.connect() as conn:
    # Create a task with an issuer
    tid = kb.create_task(conn, title='test origin receipt', assignee='worker', created_by='hermes:systems-engineer')
    print(f'Created task: {tid}')
    
    # Claim and complete
    kb.claim_task(conn, tid)
    run_id = kb.get_task(conn, tid).current_run_id
    print(f'Run ID: {run_id}')
    
    ok = kb.complete_task(conn, tid, summary='done', result='completed', expected_run_id=run_id)
    print(f'Complete result: {ok}')
    
    # Check events
    events = kb.list_events(conn, tid)
    for e in events:
        print(f'Event: {e.kind} - {e.payload}')
    
    # Check for ORIGIN_RECEIPT
    origin_events = [e for e in events if e.kind == 'ORIGIN_RECEIPT']
    print(f'ORIGIN_RECEIPT count: {len(origin_events)}')
    for e in origin_events:
        print(f'  Payload: {e.payload}')