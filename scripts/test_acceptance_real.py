#!/usr/bin/env python3
"""
Test the acceptance criteria using the actual kanban database.
"""

import sqlite3
import sys
from pathlib import Path

def get_hermes_home():
    """Get the Hermes home directory."""
    from hermes_constants import get_hermes_home
    return get_hermes_home()

def test_acceptance_criteria():
    """Test all acceptance criteria for the issuer fix."""
    
    db_path = get_hermes_home() / "kanban.db"
    
    if not db_path.exists():
        print(f"ERROR: kanban.db not found at {db_path}")
        return
    
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    try:
        print("=== Acceptance Criteria Test ===")
        
        # Test 1: Check existing task with issuer (should be fixed)
        print("\n--- Test 1: Task with issuer (should be fixed) ---")
        task_1 = conn.execute(
            "SELECT id, title, issuer, issuer_state, created_by FROM tasks WHERE id = 't_96d76a6f'"
        ).fetchone()
        
        if task_1:
            print(f"Task: {task_1['id']} - {task_1['title']}")
            print(f"Issuer: {task_1['issuer']}")
            print(f"Issuer State: {task_1['issuer_state']}")
            print(f"Created By: {task_1['created_by']}")
            
            if task_1['issuer'] is not None and task_1['issuer'] != 'None':
                print("✅ PASS: issuer is set to address (not None)")
            else:
                print("❌ FAIL: issuer is None")
        else:
            print("Task t_96d76a6f not found")
        
        # Test 2: Check tasks with no issuer (should have ISSUER_UNKNOWN)
        print("\n--- Test 2: Tasks with no issuer (should have ISSUER_UNKNOWN) ---")
        tasks_no_issuer = conn.execute(
            """
            SELECT id, title, issuer, issuer_state, created_by 
            FROM tasks 
            WHERE (issuer = 'user' OR issuer IS NULL) AND issuer_state = 'ISSUER_UNKNOWN'
            AND status IN ('ready', 'running', 'blocked')
            LIMIT 5
            """
        ).fetchall()
        
        print(f"Found {len(tasks_no_issuer)} tasks with proper ISSUER_UNKNOWN state:")
        for task in tasks_no_issuer:
            print(f"  {task['id']}: issuer={task['issuer']}, issuer_state={task['issuer_state']}")
        
        if len(tasks_no_issuer) > 0:
            print("✅ PASS: Found tasks with proper ISSUER_UNKNOWN state")
        else:
            print("ℹ️  No tasks with ISSUER_UNKNOWN found (may be normal)")
        
        # Test 3: Verify no tasks have NULL issuer with RESOLVED state
        print("\n--- Test 3: NULL issuer with RESOLVED state (should be impossible) ---")
        broken_tasks = conn.execute(
            """
            SELECT COUNT(*) as count, id, title, issuer, issuer_state, created_by
            FROM tasks 
            WHERE issuer IS NULL AND issuer_state = 'RESOLVED'
            AND status IN ('ready', 'running', 'blocked')
            """
        ).fetchone()
        
        if broken_tasks['count'] == 0:
            print("✅ PASS: No tasks have NULL issuer with RESOLVED state")
        else:
            print(f"❌ FAIL: Found {broken_tasks['count']} tasks with NULL issuer and RESOLVED state:")
            # Show details if any found
            if broken_tasks['count'] > 0:
                tasks = conn.execute(
                    """
                    SELECT id, title, issuer, issuer_state, created_by 
                    FROM tasks 
                    WHERE issuer IS NULL AND issuer_state = 'RESOLVED'
                    AND status IN ('ready', 'running', 'blocked')
                    """
                ).fetchall()
                for task in tasks:
                    print(f"  {task['id']}: {task['title']}")
                    print(f"    issuer={task['issuer']}, issuer_state={task['issuer_state']}")
        
        # Test 4: Check backfill results
        print("\n--- Test 4: Backfill results ---")
        
        # Count tasks with issuer set (should be more than before)
        tasks_with_issuer = conn.execute(
            """
            SELECT COUNT(*) as count
            FROM tasks 
            WHERE issuer IS NOT NULL AND issuer != 'user'
            AND status IN ('ready', 'running', 'blocked')
            """
        ).fetchone()
        
        print(f"Tasks with issuer set: {tasks_with_issuer['count']}")
        
        # Count tasks with created_by set to profile name
        from hermes_cli.profiles import current_profile_name
        profile_name = current_profile_name("worker") or "default"
        
        tasks_with_profile = conn.execute(
            """
            SELECT COUNT(*) as count
            FROM tasks 
            WHERE created_by = ?
            AND status IN ('ready', 'running', 'blocked')
            """,
            (profile_name,)
        ).fetchone()
        
        print(f"Tasks with created_by set to '{profile_name}': {tasks_with_profile['count']}")
        
        if tasks_with_profile['count'] > 0:
            print("✅ PASS: Some tasks have created_by set to profile name")
        else:
            print("ℹ️  No tasks with created_by set to profile name (may be normal)")
        
        # Test 5: Check the hierarchy is maintained
        print("\n--- Test 5: Hierarchy check ---")
        
        hierarchy_tasks = conn.execute(
            """
            SELECT id, title, issuer, issuer_state, created_by
            FROM tasks 
            WHERE issuer IS NOT NULL AND issuer != 'user'
            AND status IN ('ready', 'running', 'blocked')
            LIMIT 3
            """
        ).fetchall()
        
        print("Examples of proper hierarchy:")
        for task in hierarchy_tasks:
            print(f"  {task['id']}:")
            print(f"    issuer={task['issuer']} (address)")
            print(f"    created_by={task['created_by']} (profile)")
            print(f"    issuer_state={task['issuer_state']} (resolution state)")
        
        print("\n=== All Tests Completed ===")
        
    finally:
        conn.close()

if __name__ == "__main__":
    test_acceptance_criteria()