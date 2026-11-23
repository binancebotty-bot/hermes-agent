#!/usr/bin/env python3
"""
Test the fix by checking the existing problematic task
"""

import sqlite3
import sys
from pathlib import Path

def get_hermes_home():
    """Get the Hermes home directory."""
    from hermes_constants import get_hermes_home
    return get_hermes_home()

def test_backfill_fix():
    """Test the backfill functionality for existing tasks."""
    
    db_path = get_hermes_home() / "kanban.db"
    
    if not db_path.exists():
        print(f"ERROR: kanban.db not found at {db_path}")
        return
    
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    try:
        # Count tasks with the issue before fix
        print("=== Before Fix ===")
        broken_tasks = conn.execute(
            """
            SELECT id, title, issuer, issuer_state, created_by 
            FROM tasks 
            WHERE issuer IS NULL AND issuer_state = 'RESOLVED' AND created_by IS NOT NULL
            """
        ).fetchall()
        
        print(f"Found {len(broken_tasks)} broken tasks:")
        for task in broken_tasks:
            print(f"  {task['id']}: {task['title']}")
            print(f"    issuer={task['issuer']}, issuer_state={task['issuer_state']}, created_by={task['created_by']}")
        
        # Test the fix on one task
        if broken_tasks:
            task_to_fix = broken_tasks[0]
            print(f"\n=== Testing Fix on Task {task_to_fix['id']} ===")
            
            # Simulate what the fix should do
            from hermes_cli.profiles import current_profile_name
            profile_name = current_profile_name("worker") or "default"
            
            print(f"Profile name: {profile_name}")
            
            # Check if created_by should be updated
            if task_to_fix['created_by'] != profile_name:
                print(f"Created_by should be updated from '{task_to_fix['created_by']}' to '{profile_name}'")
                
                # Simulate the update
                conn.execute(
                    """
                    UPDATE tasks 
                    SET created_by = ? 
                    WHERE id = ?
                    """,
                    (profile_name, task_to_fix['id'])
                )
                conn.commit()
                
                # Verify the fix
                updated_task = conn.execute(
                    "SELECT id, issuer, issuer_state, created_by FROM tasks WHERE id = ?",
                    (task_to_fix['id'],)
                ).fetchone()
                
                print(f"After fix:")
                print(f"  issuer={updated_task['issuer']}")
                print(f"  issuer_state={updated_task['issuer_state']}")
                print(f"  created_by={updated_task['created_by']}")
                
                if updated_task['issuer'] is None and updated_task['issuer_state'] == 'RESOLVED':
                    print("❌ Still broken: issuer is NULL but issuer_state is RESOLVED")
                    print("   This task needs the issuer field to be populated from created_by")
                else:
                    print("✅ Fixed: issuer or issuer_state is correct")
            else:
                print("✅ created_by is already correct")
        else:
            print("No broken tasks found to test")
            
    finally:
        conn.close()

if __name__ == "__main__":
    test_backfill_fix()