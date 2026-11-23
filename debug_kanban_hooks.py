#!/usr/bin/env python3
"""
Debug test for kanban worker plugin hook issue.
"""

import os
import sys
import tempfile
from pathlib import Path

# Add the project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

def debug_hook_collection():
    """Debug the hook collection process step by step."""
    
    from unittest.mock import Mock, MagicMock
    from gateway.session_kanban import _run_task_turns
    
    # Create a mock agent
    mock_agent = Mock()
    mock_agent.session_id = "test_session_id"
    mock_agent.run_conversation.return_value = {"final_response": "test response"}
    
    # Mock the _collect_pre_llm_call_context function to see if it's called
    original_collect = None
    try:
        from agent.turn_context import _collect_pre_llm_call_context
        original_collect = _collect_pre_llm_call_context
        
        def mock_collect(*args, **kwargs):
            print("🔍 _collect_pre_llm_call_context called with:")
            print(f"  args: {args}")
            print(f"  kwargs: {kwargs}")
            
            # Try to import and call the real function
            try:
                result = original_collect(*args, **kwargs)
                print(f"  📋 Real result: {result}")
                return result
            except Exception as e:
                print(f"  ❌ Real function failed: {e}")
                # Return a mock result to test the flow
                return "SUPERPOWERS BOOTSTRAP ACTIVE"
        
        # Replace the function
        import agent.turn_context
        agent.turn_context._collect_pre_llm_call_context = mock_collect
        
    except ImportError as e:
        print(f"❌ Cannot import _collect_pre_llm_call_context: {e}")
        return False
    
    # Create test frame and context
    frame = {
        'text': 'work kanban task t_123',
        'policy': {
            'source': 'kanban',
            'kanban_json': '{"task_id": "t_123", "context": "test context", "board": "test", "run_id": 1, "claim_lock": "test_lock", "workspace": "/tmp", "workspaces_root": "/tmp", "profile": "default", "skills": [], "goal_mode": false, "goal_max_turns": null, "goal_text": "test goal", "max_runtime_seconds": 3600, "tenant": "default"}'
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
    
    # Call the implementation with debug hooks
    try:
        result = _run_task_turns(mock_agent, frame, history, context)
        
        # Check if the agent.run_conversation was called with a prompt containing the marker
        call_args = mock_agent.run_conversation.call_args
        if call_args:
            prompt = call_args[0][0]  # First positional argument is the prompt
            print(f"\n📝 Final prompt: {prompt}")
            
            if "SUPERPOWERS BOOTSTRAP ACTIVE" in prompt:
                print("✅ SUCCESS: Bootstrap marker found in prompt!")
                return True
            else:
                print("❌ FAILURE: Bootstrap marker not found in prompt")
                return False
        else:
            print("❌ FAILURE: agent.run_conversation was not called")
            return False
            
    except Exception as e:
        print(f"❌ FAILURE: Error running _run_task_turns: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        # Restore original function if we mocked it
        if original_collect:
            agent.turn_context._collect_pre_llm_call_context = original_collect

if __name__ == "__main__":
    print("Debugging kanban worker plugin hooks...")
    print("=" * 50)
    
    success = debug_hook_collection()
    
    print("\n" + "=" * 50)
    if success:
        print("✅ SUCCESS: Hook collection works!")
    else:
        print("❌ FAILURE: Hook collection has issues")
    sys.exit(0 if success else 1)