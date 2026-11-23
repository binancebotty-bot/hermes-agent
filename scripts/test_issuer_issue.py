#!/usr/bin/env python3
"""
Test script to reproduce the issuer resolution issue and verify the fix.
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
from hermes_cli.profiles import current_profile_name

def test_issuer_resolution():
    """Test issuer resolution with different scenarios."""
    
    # Create a temporary database
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        db_path = tmp_db.name
    
    try:
        conn = sqlite3.connect(db_path)
        
        # Test scenario 1: --issuer provided
        print("=== Test 1: --issuer session:test123 ===")
        task_id_1 = create_task(
            conn,
            title="Test with issuer",
            issuer="session:test123",
            issuer_state="RESOLVED"
        )
        
        # Check the result
        result = conn.execute(
            "SELECT id, issuer, issuer_state, created_by FROM tasks WHERE id = ?",
            (task_id_1,)
        ).fetchone()
        
        print(f"Task ID: {result['id']}")
        print(f"Issuer: {result['issuer']}")
        print(f"Issuer State: {result['issuer_state']}")
        print(f"Created By: {result['created_by']}")
        
        # Test scenario 2: --created-by provided
        print("\n=== Test 2: --created-by hermes:systems-engineer ===")
        task_id_2 = create_task(
            conn,
            title="Test with created_by",
            created_by="hermes:systems-engineer",
            issuer_state="RESOLVED"
        )
        
        result = conn.execute(
            "SELECT id, issuer, issuer_state, created_by FROM tasks WHERE id = ?",
            (task_id_2,)
        ).fetchone()
        
        print(f"Task ID: {result['id']}")
        print(f"Issuer: {result['issuer']}")
        print(f"Issuer State: {result['issuer_state']}")
        print(f"Created By: {result['created_by']}")
        
        # Test scenario 3: no issuer flags
        print("\n=== Test 3: No issuer flags ===")
        task_id_3 = create_task(
            conn,
            title="Test no issuer",
            issuer="user",
            issuer_state="ISSUER_UNKNOWN"
        )
        
        result = conn.execute(
            "SELECT id, issuer, issuer_state, created_by FROM tasks WHERE id = ?",
            (task_id_3,)
        ).fetchone()
        
        print(f"Task ID: {result['id']}")
        print(f"Issuer: {result['issuer']}")
        print(f"Issuer State: {result['issuer_state']}")
        print(f"Created By: {result['created_by']}")
        
        # Check for the issue
        print("\n=== Issue Detection ===")
        for task_id, scenario in [(task_id_1, "with --issuer"), (task_id_2, "with --created-by"), (task_id_3, "no flags")]:
            result = conn.execute(
                "SELECT issuer, issuer_state, created_by FROM tasks WHERE id = ?",
                (task_id,)
            ).fetchone()
            
            if result['issuer'] is None and result['issuer_state'] == 'RESOLVED':
                print(f"❌ ISSUE in {scenario}: issuer is NULL but issuer_state is RESOLVED")
                print(f"   Address is in created_by: {result['created_by']}")
            elif result['created_by'] is None:
                print(f"❌ ISSUE in {scenario}: created_by is NULL")
            else:
                print(f"✅ OK in {scenario}")
                
    finally:
        conn.close()
        os.unlink(db_path)

if __name__ == "__main__":
    test_issuer_resolution()