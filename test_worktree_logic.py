#!/usr/bin/env python3
"""Simple test to verify worktree environment resolution logic."""

import tempfile
from pathlib import Path
import sys

# Add the project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from pm.environments import install_key

def test_worktree_resolution_logic():
    """Test the core worktree resolution logic without directory creation."""
    
    print("Testing worktree resolution logic...")
    
    # Create a temporary directory structure
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        
        # Create parent checkout directory
        parent_checkout = temp_path / "test-repo"
        parent_checkout.mkdir()
        
        # Create worktree directory under .worktrees
        worktrees_dir = parent_checkout / ".worktrees"
        worktrees_dir.mkdir()
        worktree_dir = worktrees_dir / "t_test123"
        worktree_dir.mkdir()
        
        print(f"Parent checkout: {parent_checkout}")
        print(f"Worktree: {worktree_dir}")
        
        # Test the core fix - install keys should be the same
        parent_key = install_key(parent_checkout)
        worktree_key = install_key(worktree_dir)
        
        print(f"Parent install key: {parent_key}")
        print(f"Worktree install key: {worktree_key}")
        print(f"Keys match: {parent_key == worktree_key}")
        
        # This is the key assertion - worktrees should share parent's install key
        assert parent_key == worktree_key, f"Expected keys to match, but parent={parent_key}, worktree={worktree_key}"
        
        # Test that non-worktree paths are different
        other_dir = temp_path / "other-project"
        other_dir.mkdir()
        other_key = install_key(other_dir)
        
        print(f"Other project key: {other_key}")
        print(f"Other project different: {parent_key != other_key}")
        
        assert parent_key != other_key, "Different projects should have different keys"
        
        print("✅ Worktree resolution logic is working correctly!")
        return True

if __name__ == "__main__":
    try:
        test_worktree_resolution_logic()
        print("\n🎉 SUCCESS: Worktree environment resolution fix is working!")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ FAILED: {e}")
        sys.exit(1)