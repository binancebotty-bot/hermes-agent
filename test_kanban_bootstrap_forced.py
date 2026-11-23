#!/usr/bin/env python3
"""
Test kanban worker with forced bootstrap text to verify the fix works.
"""

import os
import sys
from pathlib import Path
from unittest.mock import Mock, patch

# Add the project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

def test_kanban_worker_with_bootstrap():
    """Test kanban worker with forced bootstrap text."""
    
    from gateway.session_kanban import _run_task_turns
    
    # Create a mock agent
    mock_agent = Mock()
    mock_agent.session_id = "test_session_id"
    mock_agent.run_conversation.return_value = {"final_response": "test response"}
    
    # Mock the _collect_pre_llm_call_context to return bootstrap text
    with patch('agent.turn_context._collect_pre_llm_call_context') as mock_collect:
        mock_collect.return_value = "SUPERPOWERS BOOTSTRAP ACTIVE — load all three before acting, every action:\n1. GRAPHIFY: structural questions -> graph query before reading files.\n2. SUPERPOWERS: load the matching skill; an unloaded skill is not in force.\n3. PONYTAIL: smallest change that fully works — reuse, delete, no new daemon.\nNON-NEGOTIABLE: the Hermes coordinator does NOT write code — the GPT seats do, and all code is reviewed through a production lane.\nEvery seat's GitHub issue IS its wake address and must wake that seat infallibly. A claim with no command output is UNKNOWN."
        
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
        
        # Call the implementation
        try:
            result = _run_task_turns(mock_agent, frame, history, context)
            
            # Check if the agent.run_conversation was called with a prompt containing the marker
            call_args = mock_agent.run_conversation.call_args
            if call_args:
                prompt = call_args[0][0]  # First positional argument is the prompt
                print(f"Generated prompt: {prompt}")
                
                if "SUPERPOWERS BOOTSTRAP ACTIVE" in prompt:
                    print("✅ SUCCESS: Bootstrap marker found in prompt!")
                    print("✅ The fix works when hooks return the expected text!")
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

if __name__ == "__main__":
    print("Testing kanban worker with forced bootstrap text...")
    print("=" * 60)
    
    success = test_kanban_worker_with_bootstrap()
    
    print("\n" + "=" * 60)
    if success:
        print("✅ SUCCESS: The fix works! The issue is with hook registration.")
        print("Next step: Ensure superpowers bootstrap plugin is loaded for kanban workers.")
    else:
        print("❌ FAILURE: Even forced bootstrap text doesn't work")
    sys.exit(0 if success else 1)