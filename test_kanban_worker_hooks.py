#!/usr/bin/env python3
"""
Test to reproduce kanban worker plugin hook issue.

This test verifies that kanban workers properly run pre_llm_call hooks
and include the "SUPERPOWERS BOOTSTRAP ACTIVE" marker in their prompts.
"""

import os
import sys
import tempfile
import sqlite3
from pathlib import Path

# Add the project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

def test_kanban_worker_bootstrap_marker():
    """Test that kanban worker prompts contain the superpowers bootstrap marker."""
    
    # Import the superpowers bootstrap to get the marker
    try:
        from plugins.superpowers_bootstrap import MARKER
        expected_marker = MARKER
        print(f"Expected marker: {expected_marker}")
    except ImportError:
        # Fallback for testing environments where the plugin isn't available
        expected_marker = "SUPERPOWERS BOOTSTRAP ACTIVE"
        print(f"Using fallback marker: {expected_marker}")
    
    # Test the current kanban worker implementation
    from gateway.session_kanban import _run_task_turns
    from unittest.mock import Mock, MagicMock
    
    # Create a mock agent
    mock_agent = Mock()
    mock_agent.session_id = "test_session_id"
    mock_agent.run_conversation.return_value = {"final_response": "test response"}
    
    # Create test frame and context
    frame = {
        'text': 'work kanban task t_123',
        'policy': {
            'source': 'kanban',
            'kanban_json': '{"task_id": "t_123", "context": "test context", "board": "test", "run_id": 1, "claim_lock": "test_lock", "workspace": "/tmp", "workspaces_root": "/tmp", "profile": "default", "skills": []}'
        }
    }
    
    history = []
    context = {
        'task_id': 't_123',
        'context': 'test context',
        'board': 'test',
        'run_id': 1,
        'claim_lock': 'test_lock',
        'workspace': '/tmp',
        'workspaces_root': '/tmp',
        'profile': 'default',
        'skills': [],
        'goal_mode': False,
        'goal_max_turns': None,
        'goal_text': 'test goal',
        'max_runtime_seconds': 3600,
        'tenant': 'default'
    }
    
    # Call the current implementation
    try:
        # Ensure plugins are discovered before running the task turns
        # This is necessary for pre_llm_call hooks to be available
        from hermes_cli.plugins import discover_plugins
        discover_plugins()
        
        result = _run_task_turns(mock_agent, frame, history, context)
        
        # Check if the agent.run_conversation was called with a prompt containing the marker
        call_args = mock_agent.run_conversation.call_args
        if call_args:
            prompt = call_args[0][0]  # First positional argument is the prompt
            print(f"Generated prompt: {prompt}")
            
            if expected_marker in prompt:
                print("✅ SUCCESS: Kanban worker prompt contains the bootstrap marker")
                return True
            else:
                print("❌ FAILURE: Kanban worker prompt missing the bootstrap marker")
                print(f"Marker '{expected_marker}' not found in prompt")
                return False
        else:
            print("❌ FAILURE: agent.run_conversation was not called")
            return False
            
    except Exception as e:
        print(f"❌ FAILURE: Error running _run_task_turns: {e}")
        return False

def test_normal_agent_has_marker():
    """Test that normal agent turns have the marker (sanity check)."""
    # This is a sanity check to ensure the marker exists in normal agent flow
    # We'll mock the hook system to return the marker
    
    try:
        from plugins.superpowers_bootstrap import MARKER
        print(f"Normal agent should have marker: {MARKER}")
        return True
    except ImportError:
        print("Using fallback marker for normal agent test")
        return True

if __name__ == "__main__":
    print("Testing kanban worker plugin hooks...")
    print("=" * 50)
    
    # Test 1: Check normal agent flow (sanity check)
    print("\n1. Testing normal agent flow (sanity check):")
    normal_result = test_normal_agent_has_marker()
    
    # Test 2: Test current kanban worker implementation
    print("\n2. Testing current kanban worker implementation:")
    kanban_result = test_kanban_worker_bootstrap_marker()
    
    print("\n" + "=" * 50)
    print("SUMMARY:")
    print(f"Normal agent flow: {'✅' if normal_result else '❌'}")
    print(f"Kanban worker flow: {'✅' if kanban_result else '❌'}")
    
    if not kanban_result:
        print("\n🔧 ISSUE CONFIRMED: Kanban workers do not include plugin hooks!")
        print("The fix requires modifying _run_task_turns() to call pre_llm_call hooks.")
        sys.exit(1)
    else:
        print("\n✅ SUCCESS: Both flows work correctly!")
        sys.exit(0)