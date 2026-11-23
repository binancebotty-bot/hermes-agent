#!/usr/bin/env python3
"""
Detailed debug test for _run_task_turns function.
"""

import os
import sys
from pathlib import Path
from unittest.mock import Mock, patch

# Add the project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

def debug_run_task_turns():
    """Debug _run_task_turns function step by step."""
    
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
    
    # Mock _collect_pre_llm_call_context to see what it returns
    with patch('agent.turn_context._collect_pre_llm_call_context') as mock_collect:
        mock_collect.return_value = "SUPERPOWERS BOOTSTRAP ACTIVE — load all three before acting, every action:\n1. GRAPHIFY: structural questions -> graph query before reading files.\n2. SUPERPOWERS: load the matching skill; an unloaded skill is not in force.\n3. PONYTAIL: smallest change that fully works — reuse, delete, no new daemon.\nNON-NEGOTIABLE: the Hermes coordinator does NOT write code — the GPT seats do, and all code is reviewed through a production lane.\nEvery seat's GitHub issue IS its wake address and must wake that seat infallibly. A claim with no command output is UNKNOWN."
        
        print("🔍 Mocked _collect_pre_llm_call_context to return bootstrap text")
        
        # Call the implementation
        try:
            result = _run_task_turns(mock_agent, frame, history, context)
            
            # Check if the agent.run_conversation was called with a prompt containing the marker
            call_args = mock_agent.run_conversation.call_args
            if call_args:
                prompt = call_args[0][0]  # First positional argument is the prompt
                print(f"📝 Final prompt: {prompt}")
                print(f"📝 Prompt length: {len(prompt)}")
                
                if "SUPERPOWERS BOOTSTRAP ACTIVE" in prompt:
                    print("✅ SUCCESS: Bootstrap marker found in prompt!")
                    return True
                else:
                    print("❌ FAILURE: Bootstrap marker not found in prompt")
                    print("🔍 Debug info:")
                    print(f"  - Prompt contains 'work kanban task': {'work kanban task' in prompt}")
                    print(f"  - Prompt contains 'test context': {'test context' in prompt}")
                    print(f"  - Prompt starts with bootstrap: {prompt.startswith('SUPERPOWERS')}")
                    
                    # Show first 500 chars
                    print(f"  - First 500 chars: {repr(prompt[:500])}")
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
    print("Detailed debug test for _run_task_turns function...")
    print("=" * 60)
    
    success = debug_run_task_turns()
    
    print("\n" + "=" * 60)
    if success:
        print("✅ SUCCESS: _run_task_turns works with mocked hooks!")
    else:
        print("❌ FAILURE: _run_task_turns has issues even with mocked hooks")
    sys.exit(0 if success else 1)