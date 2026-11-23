#!/usr/bin/env python3
"""Test script to verify worktree environment resolution works in practice."""

import tempfile
from pathlib import Path
import json
import sys
import os

# Add the project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from pm.environments import install_key, install_state_dir

def test_worktree_environment_resolution():
    """Test that demonstrates worktree environment resolution works."""
    
    print("Testing worktree environment resolution...")
    
    # Create a temporary directory structure that mimics a real repo with worktree
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
        
        print(f"Created parent checkout: {parent_checkout}")
        print(f"Created worktree: {worktree_dir}")
        
        # Calculate install keys - should be the same after fix
        parent_key = install_key(parent_checkout)
        worktree_key = install_key(worktree_dir)
        
        print(f"Parent install key: {parent_key}")
        print(f"Worktree install key: {worktree_key}")
        print(f"Keys match: {parent_key == worktree_key}")
        
        # Create a fake install record for the parent checkout
        parent_install_dir = temp_path / "installs" / parent_key
        parent_install_dir.mkdir()
        
        # Create the environments directory and a fake environment
        envs_dir = parent_install_dir / "environments"
        envs_dir.mkdir()
        fake_env = envs_dir / "test_env_001"
        fake_env.mkdir()
        
        # Create a facts.json to make it look real
        facts_file = parent_install_dir / "facts.json"
        facts_data = {
            "schema": 1,
            "packages": {
                "venv": {
                    "stamp": "abc123",
                    "environment": str(fake_env / "venv")
                }
            }
        }
        facts_file.write_text(json.dumps(facts_data, indent=2))
        
        # Both should find the same environment
        parent_state_dir = install_state_dir(parent_checkout)
        worktree_state_dir = install_state_dir(worktree_dir)
        
        print(f"Parent state directory: {parent_state_dir}")
        print(f"Worktree state directory: {worktree_state_dir}")
        print(f"State directories match: {parent_state_dir == worktree_state_dir}")
        print(f"Parent state dir exists: {parent_state_dir.exists()}")
        print(f"Worktree state dir exists: {worktree_state_dir.exists()}")
        
        # Verify the fix works
        assert parent_key == worktree_key, "Worktree should share parent's install key"
        assert parent_state_dir == worktree_state_dir, "Both should resolve to same state directory"
        assert parent_state_dir.exists(), "Parent should have environment"
        assert worktree_state_dir.exists(), "Worktree should find shared environment"
        
        print("✅ All tests passed! Worktree environment resolution is working correctly.")
        
        # Test that non-worktree paths still work differently
        other_dir = temp_path / "other-project"
        other_dir.mkdir()
        other_key = install_key(other_dir)
        other_state_dir = install_state_dir(other_dir)
        
        assert parent_key != other_key, "Different projects should have different keys"
        assert parent_state_dir != other_state_dir, "Different projects should have different state dirs"
        
        print("✅ Non-worktree isolation preserved.")
        
        return True

if __name__ == "__main__":
    try:
        test_worktree_environment_resolution()
        print("\n🎉 SUCCESS: Worktree environment resolution fix is working correctly!")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ FAILED: {e}")
        sys.exit(1)