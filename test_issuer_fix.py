#!/usr/bin/env python3
"""
Test script to verify the issuer fix works correctly.
This script directly tests the create_task function to ensure
issuer and created_by are passed separately.
"""

import os
import sys
import sqlite3
import tempfile

# Add the hermes_cli directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'hermes_cli'))

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db import KBC

def test_issuer_fix():
    """Test that create_task properly separates created_by and issuer"""
    
    # Create a temporary database for testing
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        db_path = tmp_db.name
    
    try:
        # Initialize the database
        kbc = KBC(db_path)
        kbc.init_db()
        
        # Test the create_task function with both created_by and issuer
        with kbc.connect_closing() as conn:
            task_id = kb.create_task(
                conn,
                title="Test Task",
                body="Test task for issuer fix",
                assignee="default",
                created_by="test-creator",  # This should be the actual creator
                issuer="test-issuer",       # This should be the issuer
                issuer_state="RESOLVED",
                workspace_kind="dir",
                workspace_path="/tmp/test",
            )
            
            # Query the task to verify the values
            task = kb.get_task(conn, task_id)
            
            print(f"Task ID: {task_id}")
            print(f"Title: {task.title}")
            print(f"Created by: {task.created_by}")
            print(f"Issuer: {task.issuer}")
            print(f"Issuer state: {task.issuer_state}")
            
            # Verify the fix worked
            assert task.created_by == "test-creator", f"Expected created_by='test-creator', got '{task.created_by}'"
            assert task.issuer == "test-issuer", f"Expected issuer='test-issuer', got '{task.issuer}'"
            assert task.issuer_state == "RESOLVED", f"Expected issuer_state='RESOLVED', got '{task.issuer_state}'"
            
            print("✅ Test PASSED: created_by and issuer are correctly separated")
            
    finally:
        # Clean up
        if os.path.exists(db_path):
            os.unlink(db_path)

if __name__ == "__main__":
    test_issuer_fix()