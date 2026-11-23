#!/usr/bin/env python3
"""
Check recent kanban tasks to verify the issuer issue.
"""

import sqlite3
import sys
from pathlib import Path

def get_hermes_home():
    """Get the Hermes home directory."""
    from hermes_constants import get_hermes_home
    return get_hermes_home()

def check_recent_tasks():
    """Check recent tasks for the issuer issue."""
    db_path = get_hermes_home() / "kanban.db"
    
    if not db_path.exists():
        print(f"ERROR: kanban.db not found at {db_path}")
        return
    
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    try:
        # Get recent tasks
        tasks = conn.execute(
            "SELECT id, title, status, issuer, issuer_state, created_by FROM tasks ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
        
        print("Recent tasks:")
        for task in tasks:
            print(f"\nTask: {task['id']} - {task['title']}")
            print(f"  Status: {task['status']}")
            print(f"  Issuer: {task['issuer']}")
            print(f"  Issuer State: {task['issuer_state']}")
            print(f"  Created By: {task['created_by']}")
            
            # Check for the issue
            if task['issuer'] is None and task['issuer_state'] == 'RESOLVED':
                print("  ❌ ISSUE: issuer is NULL but issuer_state is RESOLVED")
                print("     This will cause the receipt path to fall through to 'RESOLVED'")
            elif task['issuer'] is None and task['issuer_state'] == 'ISSUER_UNKNOWN':
                print("  ✅ OK: issuer is NULL and issuer_state is ISSUER_UNKNOWN")
            elif task['issuer'] is not None:
                print("  ✅ OK: issuer is set")
                
    finally:
        conn.close()

if __name__ == "__main__":
    check_recent_tasks()