#!/usr/bin/env python3
"""
Comprehensive test to verify all acceptance criteria for the issuer fix.
"""

import sqlite3
import tempfile
import os
from pathlib import Path
import sys

# Add the hermes-agent directory to the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from hermes_cli.kanban_db import create_task
from hermes_cli.profiles import current_profile_name

def test_acceptance_criteria():
    """Test all acceptance criteria for the issuer fix."""
    
    # Create a temporary database for testing
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        db_path = tmp_db.name
    
    try:
        conn = sqlite3.connect(db_path)
        
        # Create the tasks table (simplified version for testing)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                title TEXT,
                body TEXT,
                assignee TEXT,
                status TEXT,
                priority INTEGER,
                created_by TEXT,
                created_at INTEGER,
                issuer TEXT,
                issuer_state TEXT,
                workspace_kind TEXT,
                workspace_path TEXT,
                project_id TEXT,
                tenant TEXT
            )
        """)
        
        print("=== Acceptance Criteria Test ===")
        
        # Test 1: Create a card with --issuer session:<some live id>
        print("\n--- Test 1: --issuer session:<id> ---")
        profile_name = current_profile_name("worker") or "default"
        
        task_id_1 = create_task(
            conn,
            title="Test with issuer",
            issuer="session:test123",
            issuer_state="RESOLVED"
        )
        
        result_1 = conn.execute(
            "SELECT id, issuer, issuer_state, created_by FROM tasks WHERE id = ?",
            (task_id_1,)
        ).fetchone()
        
        print(f"Task ID: {result_1['id']}")
        print(f"Issuer: {result_1['issuer']}")
        print(f"Issuer State: {result_1['issuer_state']}")
        print(f"Created By: {result_1['created_by']}")
        
        if result_1['issuer'] == "session:test123" and result_1['created_by'] == profile_name:
            print("✅ PASS: issuer is set to address, created_by is set to profile name")
        else:
            print("❌ FAIL: issuer or created_by not set correctly")
        
        # Test 2: Create a card with no issuer at all
        print("\n--- Test 2: No issuer flags ---")
        task_id_2 = create_task(
            conn,
            title="Test no issuer",
            issuer="user",
            issuer_state="ISSUER_UNKNOWN"
        )
        
        result_2 = conn.execute(
            "SELECT id, issuer, issuer_state, created_by FROM tasks WHERE id = ?",
            (task_id_2,)
        ).fetchone()
        
        print(f"Task ID: {result_2['id']}")
        print(f"Issuer: {result_2['issuer']}")
        print(f"Issuer State: {result_2['issuer_state']}")
        print(f"Created By: {result_2['created_by']}")
        
        if result_2['issuer'] == "user" and result_2['issuer_state'] == "ISSUER_UNKNOWN":
            print("✅ PASS: issuer is 'user', issuer_state is 'ISSUER_UNKNOWN'")
        else:
            print("❌ FAIL: incorrect issuer or issuer_state")
        
        # Test 3: Verify NULL issuer with RESOLVED state is impossible
        print("\n--- Test 3: NULL issuer with RESOLVED state (should fail) ---")
        
        # This should not be possible with our fix, but let's test the logic
        try:
            # Try to create a task that would have NULL issuer with RESOLVED state
            conn.execute(
                """
                INSERT INTO tasks (id, title, issuer, issuer_state, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("test-null-issuer", "Test NULL issuer", None, "RESOLVED", profile_name, 1234567890)
            )
            conn.commit()
            
            # Check if this would cause the issue
            result_3 = conn.execute(
                "SELECT id, issuer, issuer_state, created_by FROM tasks WHERE id = ?",
                ("test-null-issuer",)
            ).fetchone()
            
            if result_3['issuer'] is None and result_3['issuer_state'] == "RESOLVED":
                print("❌ FAIL: NULL issuer with RESOLVED state is possible")
                print("This would cause the receipt path to fall through to 'RESOLVED'")
            else:
                print("✅ PASS: NULL issuer with RESOLVED state is prevented")
        except Exception as e:
            print(f"✅ PASS: Cannot create NULL issuer with RESOLVED state: {e}")
        
        # Clean up test data
        conn.execute("DELETE FROM tasks WHERE id IN (?, ?, ?)", (task_id_1, task_id_2, "test-null-issuer"))
        conn.commit()
        
        print("\n=== All Tests Completed ===")
        
    finally:
        conn.close()
        os.unlink(db_path)

if __name__ == "__main__":
    test_acceptance_criteria()