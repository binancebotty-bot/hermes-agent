#!/usr/bin/env python3
"""
Debug test for _collect_pre_llm_call_context function.
"""

import os
import sys
from pathlib import Path

# Add the project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

def debug_collect_context():
    """Debug what _collect_pre_llm_call_context actually returns."""
    
    print("Testing _collect_pre_llm_call_context function...")
    
    try:
        from agent.turn_context import _collect_pre_llm_call_context
        
        # Call the function and see what it returns
        result = _collect_pre_llm_call_context()
        print(f"Function result: {result}")
        print(f"Result type: {type(result)}")
        print(f"Result length: {len(result) if result else 0}")
        
        if result:
            print(f"Result content: {repr(result[:200])}")
        else:
            print("Result is empty!")
            
        # Check if it contains the bootstrap marker
        if result and "SUPERPOWERS BOOTSTRAP ACTIVE" in result:
            print("✅ Bootstrap marker found in result!")
            return True
        else:
            print("❌ Bootstrap marker NOT found in result")
            return False
            
    except Exception as e:
        print(f"❌ Error calling _collect_pre_llm_call_context: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = debug_collect_context()
    
    print("\n" + "=" * 50)
    if success:
        print("✅ SUCCESS: _collect_pre_llm_call_context works!")
    else:
        print("❌ FAILURE: _collect_pre_llm_call_context has issues")
    sys.exit(0 if success else 1)