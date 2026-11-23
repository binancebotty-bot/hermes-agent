"""Test worktree environment resolution - worktrees should share parent's environment."""

import tempfile
from pathlib import Path
import pytest

from pm.environments import install_key, install_state_dir, NoCommittedEnvironment


def test_worktree_shares_parent_install_key():
    """Test that worktree paths share parent's install key."""
    
    # Create a temporary directory structure that mimics a real repo with worktree
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        
        # Create parent checkout directory
        parent_checkout = temp_path / "hermes-agent"
        parent_checkout.mkdir()
        
        # Create worktree directory under .worktrees
        worktrees_dir = parent_checkout / ".worktrees"
        worktrees_dir.mkdir()
        worktree_dir = worktrees_dir / "t_12345678"
        worktree_dir.mkdir()
        
        # Calculate install keys - should be the same after fix
        parent_key = install_key(parent_checkout)
        worktree_key = install_key(worktree_dir)
        
        # After fix: worktree should have same install key as parent
        assert parent_key == worktree_key, f"Worktree should share parent's install key, but parent={parent_key}, worktree={worktree_key}"


def test_worktree_finds_parent_environment():
    """Test that worktree can find parent's committed environment when it exists."""
    
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        
        # Create parent checkout directory
        parent_checkout = temp_path / "hermes-agent"
        parent_checkout.mkdir()
        
        # Create worktree directory under .worktrees
        worktrees_dir = parent_checkout / ".worktrees"
        worktrees_dir.mkdir()
        worktree_dir = worktrees_dir / "t_12345678"
        worktree_dir.mkdir()
        
        # Create a fake install record for the parent checkout
        parent_install_key = install_key(parent_checkout)
        parent_install_dir = temp_path / "installs" / parent_install_key
        parent_install_dir.mkdir()
        
        # Create the environments directory and a fake environment
        envs_dir = parent_install_dir / "environments"
        envs_dir.mkdir()
        fake_env = envs_dir / "fake_env_123"
        fake_env.mkdir()
        
        # Create a facts.json to make it look real
        facts_file = parent_install_dir / "facts.json"
        facts_file.write_text('{"schema": 1, "packages": {"venv": {"stamp": "abc123"}}}')
        
        # Both should find the same environment
        parent_state_dir = install_state_dir(parent_checkout)
        worktree_state_dir = install_state_dir(worktree_dir)
        
        # Both should exist (parent has environment, worktree shares it)
        assert parent_state_dir.exists(), f"Parent should have environment at {parent_state_dir}"
        assert worktree_state_dir.exists(), f"Worktree should find shared environment at {worktree_state_dir}"
        assert parent_state_dir == worktree_state_dir, f"Both should resolve to same state directory: {parent_state_dir}"


def test_worktree_without_parent_environment_fails():
    """Test that worktree fails appropriately when parent has no environment."""
    
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        
        # Create parent checkout directory (no environment)
        parent_checkout = temp_path / "hermes-agent"
        parent_checkout.mkdir()
        
        # Create worktree directory under .worktrees
        worktrees_dir = parent_checkout / ".worktrees"
        worktrees_dir.mkdir()
        worktree_dir = worktrees_dir / "t_12345678"
        worktree_dir.mkdir()
        
        # Both should have same install key but no environment
        parent_key = install_key(parent_checkout)
        worktree_key = install_key(worktree_dir)
        assert parent_key == worktree_key
        
        # Both should fail to find environment
        parent_state_dir = install_state_dir(parent_checkout)
        worktree_state_dir = install_state_dir(worktree_dir)
        
        assert not parent_state_dir.exists()
        assert not worktree_state_dir.exists()
        assert parent_state_dir == worktree_state_dir
        
        # Both should raise NoCommittedEnvironment when trying to activate
        with pytest.raises(NoCommittedEnvironment):
            if not parent_state_dir.exists():
                raise NoCommittedEnvironment("no dependency environment is committed for this install")
        
        with pytest.raises(NoCommittedEnvironment):
            if not worktree_state_dir.exists():
                raise NoCommittedEnvironment("no dependency environment is committed for this install")


def test_non_worktree_paths_unchanged():
    """Test that non-worktree paths still work as before."""
    
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        
        # Create regular directory (not under .worktrees)
        regular_dir = temp_path / "regular-project"
        regular_dir.mkdir()
        
        # Create another directory at same level
        other_dir = temp_path / "other-project"
        other_dir.mkdir()
        
        # Should have different install keys
        key1 = install_key(regular_dir)
        key2 = install_key(other_dir)
        assert key1 != key2
        
        # Should resolve to different state directories
        state1 = install_state_dir(regular_dir)
        state2 = install_state_dir(other_dir)
        assert state1 != state2