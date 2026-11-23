#!/usr/bin/env python3
"""
Test what the real _collect_pre_llm_call_context function returns.
"""

import os
import sys
from pathlib import Path
from unittest.mock import Mock

# Add the project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

def test_real_collect_context():
    """Test what the real _collect_pre_llm_call_context function returns."""
    
    from gateway.session_kanban import _run_task_turns
    
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
    
    # Patch the prompt building to show what plugin_user_context contains
    original_run_task_turns = _run_task_turns
    
    def debug_run_task_turns(agent, frame, history, context):
        # Call the original function but intercept the prompt building
        result = original_run_task_turns(agent, frame, history, context)
        
        # Check if the agent.run_conversation was called
        call_args = agent.run_conversation.call_args
        if call_args:
            prompt = call_args[0][0]
            print(f"📝 Final prompt: {prompt}")
            
            # Check for bootstrap marker
            if "SUPERPOWERS BOOTSTRAP ACTIVE" in prompt:
                print("✅ Bootstrap marker found in prompt!")
            else:
                print("❌ Bootstrap marker NOT found in prompt")
                print("🔍 Debug info:")
                print(f"  - Prompt contains 'work kanban task': {'work kanban task' in prompt}")
                print(f"  - Prompt contains 'test context': {'test context' in prompt}")
                
        return result
    
    # Replace the function temporarily
    import gateway.session_kanban
    gateway.session_kanban._run_task_turns = debug_run_task_turns
    
    try:
        # Call the implementation
        result = _run_task_turns(mock_agent, frame, history, context)
    finally:
        # Restore the original function
        gateway.session_kanban._run_task_turns = original_run_task_turns

if __name__ == "__main__":
    print("Testing real _collect_pre_llm_call_context function...")
    print("=" * 60)
    
    test_real_collect_context()
    
    print("\n" + "=" * 60)
    print("Test completed.")