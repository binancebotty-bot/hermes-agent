#!/usr/bin/env python3
"""
Backfill script to fix existing broken tasks.
"""

import sqlite3
import sys
from pathlib import Path

def get_hermes_home():
    """Get the Hermes home directory."""
    from hermes_constants import get_hermes_home
    return get_hermes_home()

def backfill_issuer_fix():
    """Backfill existing broken tasks."""
    
    db_path = get_hermes_home() / "kanban.db"
    
    if not db_path.exists():
        print(f"ERROR: kanban.db not found at {db_path}")
        return
    
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    try:
        # Find all broken tasks that need fixing
        print("=== Finding Broken Tasks ===")
        broken_tasks = conn.execute(
            """
            SELECT id, title, issuer, issuer_state, created_by 
            FROM tasks 
            WHERE issuer IS NULL AND issuer_state = 'RESOLVED' AND created_by IS NOT NULL
            AND status IN ('ready', 'running', 'blocked')
            """
        ).fetchall()
        
        print(f"Found {len(broken_tasks)} broken tasks to fix:")
        
        if not broken_tasks:
            print("✅ No broken tasks found")
            return
            
        for task in broken_tasks:
            print(f"  {task['id']}: {task['title']}")
        
        # Get the current profile name
        from hermes_cli.profiles import current_profile_name
        profile_name = current_profile_name("worker") or "default"
        print(f"\nUsing profile name: {profile_name}")
        
        # Fix each task
        print(f"\n=== Fixing Tasks ===")
        fixed_count = 0
        
        for task in broken_tasks:
            task_id = task['id']
            old_created_by = task['created_by']
            
            # Move the address from created_by to issuer
            # created_by should be set to the profile name
            conn.execute(
                """
                UPDATE tasks 
                SET issuer = ?, 
                    created_by = ?
                WHERE id = ?
                """,
                (old_created_by, profile_name, task_id)
            )
            
            print(f"Fixed {task_id}:")
            print(f"  issuer: NULL -> {old_created_by}")
            print(f"  created_by: {old_created_by} -> {profile_name}")
            fixed_count += 1
        
        conn.commit()
        
        # Report results
        print(f"\n=== Results ===")
        print(f"Fixed {fixed_count} tasks")
        
        # Verify the fix
        print(f"\n=== Verification ===")
        remaining_broken = conn.execute(
            """
            SELECT COUNT(*) as count
            FROM tasks 
            WHERE issuer IS NULL AND issuer_state = 'RESOLVED' AND created_by IS NOT NULL
            AND status IN ('ready', 'running', 'blocked')
            """
        ).fetchone()
        
        if remaining_broken['count'] == 0:
            print("✅ All broken tasks have been fixed")
        else:
            print(f"⚠️  {remaining_broken['count']} broken tasks remain")
            
        # Show some examples of fixed tasks
        print(f"\n=== Examples of Fixed Tasks ===")
        examples = conn.execute(
            """
            SELECT id, title, issuer, issuer_state, created_by 
            FROM tasks 
            WHERE issuer IS NOT NULL AND created_by = ?
            AND status IN ('ready', 'running', 'blocked')
            LIMIT 3
            """,
            (profile_name,)
        ).fetchall()
        
        for task in examples:
            print(f"  {task['id']}: issuer={task['issuer']}, created_by={task['created_by']}")
            
    finally:
        conn.close()

if __name__ == "__main__":
    backfill_issuer_fix()