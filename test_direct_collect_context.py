#!/usr/bin/env python3
"""
Direct test of _collect_pre_llm_call_context function.
"""

import os
import sys
from pathlib import Path
from unittest.mock import Mock

# Add the project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

def test_direct_collect_context():
    """Test _collect_pre_llm_call_context directly."""
    
    # Ensure plugins are discovered
    from hermes_cli.plugins import discover_plugins
    discover_plugins()
    
    # Create a mock agent
    mock_agent = Mock()
    mock_agent.session_id = "test_session_id"
    
    try:
        from agent.turn_context import _collect_pre_llm_call_context
        
        # Call the function directly with the same arguments as in _run_task_turns
        original_user_message = 'work kanban task t_123'
        turn_id = "kanban_t_123_1"
        
        result = _collect_pre_llm_call_context(
            agent=mock_agent,
            effective_task_id="t_123",
            turn_id=turn_id,
            original_user_message=original_user_message,
            messages=[{'role': 'user', 'content': original_user_message}],
            conversation_history=[],
            blocks=[],
        )
        
        print(f"🔍 _collect_pre_llm_call_context result: {result}")
        print(f"🔍 Result type: {type(result)}")
        print(f"🔍 Result length: {len(result) if result else 0}")
        
        if result:
            print(f"🔍 Result content: {repr(result[:200])}")
            
            if "SUPERPOWERS BOOTSTRAP ACTIVE" in result:
                print("✅ Bootstrap marker found in result!")
                return True
            else:
                print("❌ Bootstrap marker NOT found in result")
                return False
        else:
            print("❌ Result is empty!")
            return False
            
    except Exception as e:
        print(f"❌ Error calling _collect_pre_llm_call_context: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    print("Direct test of _collect_pre_llm_call_context function...")
    print("=" * 60)
    
    success = test_direct_collect_context()
    
    print("\n" + "=" * 60)
    if success:
        print("✅ SUCCESS: _collect_pre_llm_call_context works!")
    else:
        print("❌ FAILURE: _collect_pre_llm_call_context has issues")
    sys.exit(0 if success else 1)