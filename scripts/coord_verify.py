#!/usr/bin/env python3
"""
Probe script to verify kanban task issuer resolution.
Usage: python scripts/coord_verify.py origin <task_id>
"""

import sys
import sqlite3
import os
from pathlib import Path

def get_hermes_home():
    """Get the Hermes home directory."""
    from hermes_constants import get_hermes_home
    return get_hermes_home()

def probe_task(task_id):
    """Probe a task's issuer resolution."""
    db_path = get_hermes_home() / "kanban.db"
    
    if not db_path.exists():
        print(f"ERROR: kanban.db not found at {db_path}")
        return
    
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    try:
        # Get task info
        task = conn.execute(
            "SELECT id, title, status, issuer, issuer_state, created_by FROM tasks WHERE id = ?",
            (task_id,)
        ).fetchone()
        
        if not task:
            print(f"ERROR: Task {task_id} not found")
            return
        
        print(f"card               {task['id']}")
        print(f"status             {task['status']}")
        print(f"issuer             {task['issuer']}")
        print(f"issuer_state       {task['issuer_state']}")
        print(f"created_by         {task['created_by']}")
        
        # Check for the issue described in the task
        if task['issuer'] is None and task['issuer_state'] == 'RESOLVED':
            print("address            MISPLACED - the address is in created_by, the issuer column is")
            print("                     empty; the receipt path reads issuer first, so a real address is unreachable")
        elif task['issuer'] is None:
            print("address            NONE (no issuer provided)")
        else:
            print("address            OK")
            
        # Check envelope records
        envelopes = conn.execute(
            """
            SELECT msgid, obligation_id, state, attempts 
            FROM undelivered_envelopes 
            WHERE msgid LIKE ? OR obligation_id LIKE ?
            """,
            (f"%{task_id}%", f"%{task_id}%")
        ).fetchall()
        
        if envelopes:
            print(f"\nENVELOPES referencing {task_id}:")
            for env in envelopes:
                print(f"  {env['msgid']} / {env['obligation_id']} - state={env['state']}, attempts={env['attempts']}")
        else:
            print(f"\nENVELOPES: none found referencing {task_id}")
            
    finally:
        conn.close()

if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "origin":
        print("Usage: python scripts/coord_verify.py origin <task_id>")
        sys.exit(1)
    
    task_id = sys.argv[2]
    probe_task(task_id)