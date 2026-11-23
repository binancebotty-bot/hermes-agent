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
from hermes_state import SessionDB
from hermes_constants import get_hermes_home

kb.init_db()

with kbc.connect() as conn:
    # Create a session for the issuer
    state = SessionDB(db_path=get_hermes_home() / "state.db")
    import uuid
    session_id = str(uuid.uuid4())
    state.create_session(session_id=session_id, source="test-agent")
    state.close()
    print(f'Created session: {session_id}')
    
    # Test 1: Task with session-based issuer (should work)
    print("\n=== Test 1: Task with session issuer ===")
    tid = kb.create_task(conn, title='test origin receipt 1', assignee='worker', issuer=f'session:{session_id}')
    print(f'Created task: {tid}')
    
    kb.claim_task(conn, tid)
    run_id = kb.get_task(conn, tid).current_run_id
    print(f'Run ID: {run_id}')
    
    ok = kb.complete_task(conn, tid, summary='done', result='completed', expected_run_id=run_id)
    print(f'Complete result: {ok}')
    
    events = kb.list_events(conn, tid)
    for e in events:
        print(f'Event: {e.kind}')
    
    origin_events = [e for e in events if e.kind == 'ORIGIN_RECEIPT']
    print(f'ORIGIN_RECEIPT count: {len(origin_events)}')
    for e in origin_events:
        print(f'  Payload: {e.payload}')
        # Check (a): exactly one ORIGIN_RECEIPT per completion, inside the completion transaction
        # Check (b): destination_row_id is a real id, never the string "None"
        delivery_id = e.payload.get('delivery_id', '')
        print(f'  delivery_id: {delivery_id}')
        print(f'  delivery_id is "None": {delivery_id == "None"}')
        print(f'  delivered: {e.payload.get("delivered")}')
        print(f'  delivery_kind: {e.payload.get("delivery_kind")}')

    # Test 2: Check review_requested and reviewer_verdict events are run-scoped
    print("\n=== Test 2: Review cycle events (run-scoped) ===")
    tid2 = kb.create_task(conn, title='test review cycle', assignee='worker', issuer=f'session:{session_id}')
    print(f'Created task: {tid2}')
    
    kb.claim_task(conn, tid2)
    run_id2 = kb.get_task(conn, tid2).current_run_id
    print(f'Implement run ID: {run_id2}')
    
    # Request review
    ok_review = kb.request_review(conn, tid2, summary='ready for review', reviewer='reviewer', expected_run_id=run_id2)
    print(f'Request review result: {ok_review}')
    
    events2 = kb.list_events(conn, tid2)
    review_requested = [e for e in events2 if e.kind == 'review_requested']
    print(f'review_requested count: {len(review_requested)}')
    for e in review_requested:
        print(f'  Payload: {e.payload}')
        print(f'  run_id: {e.run_id}')
        print(f'  run_id matches implement run: {e.run_id == run_id2}')
    
    # Claim review task
    review_claim = kb.claim_review_task(conn, tid2)
    print(f'Review claim: {review_claim}')
    
    # Complete review with PASS
    review_run_id = kb.get_task(conn, tid2).current_run_id
    print(f'Review run ID: {review_run_id}')
    
    ok_complete = kb.complete_task(conn, tid2, summary='REVIEWER_VERDICT: PASS', result='', expected_run_id=review_run_id)
    print(f'Complete review result: {ok_complete}')
    
    events2_after = kb.list_events(conn, tid2)
    reviewer_verdict = [e for e in events2_after if e.kind == 'reviewer_verdict']
    print(f'reviewer_verdict count: {len(reviewer_verdict)}')
    for e in reviewer_verdict:
        print(f'  Payload: {e.payload}')
        print(f'  run_id: {e.run_id}')
        print(f'  run_id matches review run: {e.run_id == review_run_id}')
    
    # Check parent task for verdict (should not have one since this is orphaned review)
    parent_id = conn.execute("SELECT parent_id FROM task_links WHERE child_id = ?", (tid2,)).fetchone()
    if parent_id:
        parent_id = parent_id[0]
        print(f'Parent task: {parent_id}')
        parent_events = kb.list_events(conn, parent_id)
        parent_verdict = [e for e in parent_events if e.kind == 'reviewer_verdict']
        print(f'Parent reviewer_verdict count: {len(parent_verdict)}')
    else:
        print('No parent task (orphaned review)')

    # Test 3: Full review cycle with parent fix task
    print("\n=== Test 3: Full review cycle with parent fix task ===")
    fix_task = kb.create_task(conn, title='fix something', assignee='worker', issuer=f'session:{session_id}')
    print(f'Created fix task: {fix_task}')
    
    kb.claim_task(conn, fix_task)
    fix_run = kb.get_task(conn, fix_task).current_run_id
    kb.request_review(conn, fix_task, summary='fix ready', reviewer='reviewer', expected_run_id=fix_run)
    
    review_task_id = conn.execute("SELECT child_id FROM task_links WHERE parent_id = ?", (fix_task,)).fetchone()
    if review_task_id:
        review_task_id = review_task_id[0]
        print(f'Review task: {review_task_id}')
        
        review_claim = kb.claim_review_task(conn, review_task_id)
        review_run = kb.get_task(conn, review_task_id).current_run_id
        
        ok_rev = kb.complete_task(conn, review_task_id, summary='REVIEWER_VERDICT: PASS', result='', expected_run_id=review_run)
        print(f'Complete review result: {ok_rev}')
        
        # Check fix task events
        fix_events = kb.list_events(conn, fix_task)
        fix_verdict = [e for e in fix_events if e.kind == 'reviewer_verdict']
        print(f'Fix task reviewer_verdict count: {len(fix_verdict)}')
        for e in fix_verdict:
            print(f'  Payload: {e.payload}')
            print(f'  run_id: {e.run_id}')
        
        # Check review task events
        rev_events = kb.list_events(conn, review_task_id)
        rev_verdict = [e for e in rev_events if e.kind == 'reviewer_verdict']
        print(f'Review task reviewer_verdict count: {len(rev_verdict)}')
        for e in rev_verdict:
            print(f'  Payload: {e.payload}')
            print(f'  run_id: {e.run_id}')

    # Test 4: Check completions vs receipts counts
    print("\n=== Test 4: Completions vs receipts counts ===")
    # Create multiple tasks and complete them
    for i in range(3):
        t = kb.create_task(conn, title=f'count test {i}', assignee='worker', issuer=f'session:{session_id}')
        kb.claim_task(conn, t)
        rid = kb.get_task(conn, t).current_run_id
        kb.complete_task(conn, t, summary=f'done {i}', result=f'completed {i}', expected_run_id=rid)
    
    # Count completions and receipts across all tasks
    all_tasks = kb.list_tasks(conn)
    total_completions = 0
    total_receipts = 0
    for task in all_tasks:
        task_events = kb.list_events(conn, task.id)
        task_completions = [e for e in task_events if e.kind == 'completed']
        task_receipts = [e for e in task_events if e.kind == 'ORIGIN_RECEIPT']
        total_completions += len(task_completions)
        total_receipts += len(task_receipts)
        if task_completions or task_receipts:
            print(f'  Task {task.id}: completions={len(task_completions)}, receipts={len(task_receipts)}')
    
    print(f'Total completions: {total_completions}')
    print(f'Total ORIGIN_RECEIPT: {total_receipts}')
    print(f'Counts equal: {total_completions == total_receipts}')

    # Test 5: Check duplicate prevention (complete twice)
    print("\n=== Test 5: Duplicate ORIGIN_RECEIPT prevention ===")
    tid5 = kb.create_task(conn, title='duplicate test', assignee='worker', issuer=f'session:{session_id}')
    kb.claim_task(conn, tid5)
    run_id5 = kb.get_task(conn, tid5).current_run_id
    kb.complete_task(conn, tid5, summary='first', result='first', expected_run_id=run_id5)
    
    # Check the events
    events5 = kb.list_events(conn, tid5)
    receipts5 = [e for e in events5 if e.kind == 'ORIGIN_RECEIPT']
    print(f'ORIGIN_RECEIPT count after one completion: {len(receipts5)}')
    
    # Check the dedupe query logic
    # The code checks for existing ORIGIN_RECEIPT with verdict=COMPLETE
    existing = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'ORIGIN_RECEIPT' AND json_extract(payload, '$.verdict') = 'COMPLETE' LIMIT 1",
        (tid5,)
    ).fetchone()
    print(f'Existing COMPLETE receipt found: {existing is not None}')

print("\n=== All tests completed ===")