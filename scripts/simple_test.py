#!/usr/bin/env python3
"""
Simple test to check the current behavior and verify the issue.
"""

import sqlite3
import sys
from pathlib import Path

def get_hermes_home():
    """Get the Hermes home directory."""
    from hermes_constants import get_hermes_home
    return get_hermes_home()

def check_existing_issue():
    """Check the existing issue in the database."""
    db_path = get_hermes_home() / "kanban.db"
    
    if not db_path.exists():
        print(f"ERROR: kanban.db not found at {db_path}")
        return
    
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    try:
        # Find the problematic task
        task = conn.execute(
            "SELECT id, title, issuer, issuer_state, created_by FROM tasks WHERE id = 't_96d76a6f'"
        ).fetchone()
        
        if task:
            print(f"Found task: {task['id']} - {task['title']}")
            print(f"  Issuer: {task['issuer']}")
            print(f"  Issuer State: {task['issuer_state']}")
            print(f"  Created By: {task['created_by']}")
            
            # This is the issue: issuer is NULL but issuer_state is RESOLVED
            if task['issuer'] is None and task['issuer_state'] == 'RESOLVED':
                print("  ❌ CONFIRMED ISSUE: issuer is NULL but issuer_state is RESOLVED")
                print("     This will cause the receipt path to fall through to 'RESOLVED'")
                print("     instead of using the address in created_by")
            else:
                print("  ✅ No issue found")
        else:
            print("Task t_96d76a6f not found")
            
    finally:
        conn.close()

if __name__ == "__main__":
    check_existing_issue()