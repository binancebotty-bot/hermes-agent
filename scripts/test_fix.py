#!/usr/bin/env python3
"""
Test the fix by directly calling create_task with --issuer
"""

import sqlite3
import tempfile
import os
from pathlib import Path
from unittest.mock import patch

# Add the hermes-agent directory to the path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from hermes_cli.kanban_db import create_task

def test_create_task_with_issuer():
    """Test create_task with issuer parameter."""
    
    # Create a temporary database
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
        
        # Mock current_profile_name to return a known value
        with patch('hermes_cli.profiles.current_profile_name') as mock_profile:
            mock_profile.return_value = "test-profile"
            
            # Test case 1: Create task with issuer
            print("=== Test 1: Create task with issuer ===")
            task_id = create_task(
                conn,
                title="Test with issuer",
                issuer="session:test123",
                issuer_state="RESOLVED"
            )
            
            print(f"Created task ID: {task_id}")
            
            # Check the result
            result = conn.execute(
                "SELECT id, issuer, issuer_state, created_by FROM tasks WHERE id = ?",
                (task_id,)
            ).fetchone()
            
            print(f"Issuer: {result['issuer']}")
            print(f"Issuer State: {result['issuer_state']}")
            print(f"Created By: {result['created_by']}")
            
            # Check if the fix worked
            if result['issuer'] == "session:test123" and result['created_by'] == "test-profile":
                print("✅ FIXED: issuer is set correctly and created_by is set to profile name")
            else:
                print("❌ ISSUE: fix not working")
                print(f"   Expected issuer=session:test123, created_by=test-profile")
                print(f"   Got issuer={result['issuer']}, created_by={result['created_by']}")
                
    finally:
        conn.close()
        os.unlink(db_path)

if __name__ == "__main__":
    test_create_task_with_issuer()