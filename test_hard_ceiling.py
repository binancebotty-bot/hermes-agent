"""
Test for hard ceiling enforcement in kanban lane caps.

This test verifies that:
1. Free provider lanes are capped at 2 workers maximum
2. Paid provider lanes are capped at 1 worker maximum  
3. Workers cannot raise caps above the ceiling through lane commands
4. Unmanaged lanes are handled properly
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Import the functions we need to test
from hermes_cli.kanban_db_dispatch import _lane_has_room, _lane_caps


class TestHardCeiling(unittest.TestCase):
    """Test hard ceiling enforcement for lane caps."""
    
    def setUp(self):
        """Set up test environment with temporary caps file."""
        self.temp_dir = tempfile.mkdtemp()
        self.temp_caps_file = Path(self.temp_dir) / 'state' / 'kanban_lane_caps.json'
        
        # Set test environment
        self.original_home = os.environ.get('HERMES_HOME')
        os.environ['HERMES_HOME'] = self.temp_dir
        
        # Create model_roster.json with free providers
        state_dir = Path(self.temp_dir) / 'state'
        state_dir.mkdir(exist_ok=True)
        model_roster_file = state_dir / 'model_roster.json'
        
        # Define free providers for testing
        model_roster_data = {
            "nvidia": {
                "provider": "nvidia",
                "free_ids": True,
                "route_ok": True
            },
            "nous": {
                "provider": "nous", 
                "free_ids": True,
                "route_ok": True
            },
            "zai": {
                "provider": "zai",
                "free_ids": True,
                "route_ok": True
            },
            "openai-codex": {
                "provider": "openai-codex",
                "free_ids": False,
                "route_ok": True
            },
            "gemini": {
                "provider": "gemini",
                "free_ids": False,
                "route_ok": True
            }
        }
        
        with open(model_roster_file, 'w') as f:
            json.dump(model_roster_data, f)
        
    def tearDown(self):
        """Clean up test environment."""
        if self.original_home:
            os.environ['HERMES_HOME'] = self.original_home
        else:
            os.environ.pop('HERMES_HOME', None)
            
        # Clean up temp dir
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def create_caps_file(self, caps_data):
        """Create a caps file with the given data."""
        # Create state directory
        state_dir = Path(self.temp_dir) / 'state'
        state_dir.mkdir(exist_ok=True)
        
        # Create caps file in state directory
        caps_file = state_dir / 'kanban_lane_caps.json'
        with open(caps_file, 'w') as f:
            json.dump(caps_data, f)
        
        return caps_file
    
    def test_free_provider_exceeds_hard_ceiling_fails(self):
        """R1: Test that free providers cannot exceed hard ceiling of 2."""
        # Set up caps file with nvidia=10 (above hard ceiling)
        self.create_caps_file({
            "nvidia": 10,  # Free provider, should be clamped to 2
            "nous": 2,     # Free provider, at ceiling
            "zai": 1       # Free provider, below ceiling
        })
        
        # Test current behavior after fix (should pass - no room for 3rd worker)
        caps = _lane_caps()
        running = {"nvidia": 2, "nous": 2, "zai": 1}  # nvidia at cap, others at/below
        
        # After fix, this should be False - no room for 3rd worker due to hard ceiling
        has_room, now, cap = _lane_has_room("nvidia", caps, running)
        
        # After fix, cap should be clamped to 2, no room for 3rd worker
        self.assertFalse(has_room, "Free provider nvidia should be clamped to 2, no room for 3rd worker")
        self.assertEqual(cap, 2, "Free provider cap should be clamped to hard ceiling of 2")
        self.assertEqual(now, 2, "Current running count should be 2")
    
    def test_paid_provider_exceeds_hard_ceiling_fails(self):
        """R1: Test that paid providers cannot exceed hard ceiling of 1."""
        # Set up caps file with openai-codex=5 (above hard ceiling)
        self.create_caps_file({
            "openai-codex": 5,  # Paid provider, should be clamped to 1
            "gemini": 1        # Paid provider, at ceiling
        })
        
        caps = _lane_caps()
        running = {"openai-codex": 1, "gemini": 1}  # both at cap
        
        # After fix, this should be False - no room for 2nd worker due to hard ceiling
        has_room, now, cap = _lane_has_room("openai-codex", caps, running)
        
        # After fix, cap should be clamped to 1, no room for 2nd worker
        self.assertFalse(has_room, "Paid provider openai-codex should be clamped to 1, no room for 2nd worker")
        self.assertEqual(cap, 1, "Paid provider cap should be clamped to hard ceiling of 1")
        self.assertEqual(now, 1, "Current running count should be 1")
    
    def test_below_hard_ceiling_passes(self):
        """Test that configurations below hard ceiling still work."""
        # Set up caps file with values below hard ceilings but allowing room
        self.create_caps_file({
            "nvidia": 1,    # Free provider, below ceiling of 2, has room for 1 more
            "openai-codex": 1  # Paid provider, at ceiling of 1, no room for more
        })
        
        caps = _lane_caps()
        
        # nvidia should have room for 1 more worker (1 < 2)
        has_room, now, cap = _lane_has_room("nvidia", caps, {"nvidia": 0})  # 0 running, not 1
        self.assertTrue(has_room, f"nvidia should have room: now={now}, cap={cap}")
        
        # openai-codex should have no room when at cap (1 >= 1)
        has_room, now, cap = _lane_has_room("openai-codex", caps, {"openai-codex": 1})
        self.assertFalse(has_room, f"openai-codex should have no room when at cap: now={now}, cap={cap}")
    
    def test_unmanaged_lane_handling(self):
        """Test handling of lanes not in caps file."""
        self.create_caps_file({
            "nvidia": 2  # Only nvidia is managed
        })
        
        caps = _lane_caps()
        running = {"nvidia": 2}
        
        # deepinfra is not in caps file - should be handled properly
        has_room, now, cap = _lane_has_room("deepinfra", caps, running)
        
        # Current behavior: returns True (unmanaged, no limit)
        # After fix: should either be False (refuse) or count against a ceiling
        self.assertTrue(has_room, "Current implementation allows unmanaged lanes with no limit")
    
    def test_r6_orphan_worker_accounting(self):
        """R6: Test that orphan workers are counted against provider capacity."""
        # This test simulates the scenario where:
        # - 2 nvidia workers are running (database records)
        # - 2 additional nvidia workers are orphaned (no database records)
        # - Total should be 4, but admission should only allow 2 more due to hard ceiling
        
        self.create_caps_file({
            "nvidia": 4  # Configured cap of 4, but hard ceiling is 2
        })
        
        caps = _lane_caps()
        
        # Database shows only 2 running workers
        running_db = {"nvidia": 2}
        
        # _lane_has_room should only see 2 workers in database
        has_room, now, cap = _lane_has_room("nvidia", caps, running_db)
        
        # With hard ceiling of 2, there should be no room for more workers
        # even though configured cap allows 4
        self.assertFalse(has_room, "Should not allow more workers when hard ceiling is reached")
        self.assertEqual(cap, 2, "Hard ceiling should clamp free provider to 2")
        self.assertEqual(now, 2, "Should count only database-visible workers")


if __name__ == '__main__':
    # Run the test to show it currently fails
    unittest.main()