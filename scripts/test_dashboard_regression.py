#!/usr/bin/env python3
"""
Regression test for dashboard port drift fix.

This script demonstrates the issue where the funnel routes point to dead ports
and shows how the canary script detects the problem and validates the fix.

Usage:
    python scripts/test_dashboard_regression.py
"""

import subprocess
import sys
import json
from pathlib import Path

# Import our port configuration and scripts
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.dashboard_port import get_dashboard_port, get_dashboard_url, get_funnel_config
from scripts.dashboard_canary import DashboardCanary


def run_command(command: list) -> tuple:
    """Run a shell command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except Exception as e:
        return -1, "", str(e)


def test_canary_with_wrong_port():
    """Test the canary script with a wrong port (should fail)."""
    print("=== Testing Canary with Wrong Port (8765) ===")
    
    # Test against the old dead port
    wrong_url = "http://127.0.0.1:8765"
    print(f"Testing: {wrong_url}")
    
    canary = DashboardCanary(public_url=wrong_url, timeout=5)
    success, message = canary.run_full_check()
    
    if not success:
        print(f"✓ Canary correctly detected failure: {message}")
        return True
    else:
        print(f"✗ Canary should have failed but passed: {message}")
        return False


def test_canary_with_correct_port():
    """Test the canary script with the correct port (should pass if dashboard is running)."""
    print("\n=== Testing Canary with Correct Port ===")
    
    # Test against the current dashboard port
    correct_url = get_dashboard_url()
    print(f"Testing: {correct_url}")
    
    canary = DashboardCanary(public_url=correct_url, timeout=5)
    success, message = canary.run_full_check()
    
    if success:
        print(f"✓ Canary correctly detected success: {message}")
        return True
    else:
        print(f"⚠️  Canary failed (dashboard may not be running): {message}")
        return False


def test_canary_with_funnel_url():
    """Test the canary script with the funnel URL (should work after fix)."""
    print("\n=== Testing Canary with Funnel URL ===")
    
    funnel_config = get_funnel_config()
    funnel_url = f"https://hermes-webhook.tail851b82.ts.net:{funnel_config['https_port']}"
    print(f"Testing: {funnel_url}")
    
    canary = DashboardCanary(public_url=funnel_url, timeout=10)
    success, message = canary.run_full_check()
    
    if success:
        print(f"✓ Funnel URL is working: {message}")
        return True
    else:
        print(f"✗ Funnel URL not working (may need update): {message}")
        return False


def test_port_configuration():
    """Test the port configuration consistency."""
    print("\n=== Testing Port Configuration Consistency ===")
    
    dashboard_port = get_dashboard_port()
    dashboard_url = get_dashboard_url()
    funnel_config = get_funnel_config()
    
    print(f"Dashboard port: {dashboard_port}")
    print(f"Dashboard URL: {dashboard_url}")
    print(f"Funnel HTTPS port: {funnel_config['https_port']}")
    print(f"Funnel alt port: {funnel_config['alt_port']}")
    
    # Check if the dashboard port is the expected value
    if dashboard_port == 9120:
        print("✓ Dashboard port is correctly set to 9120")
        return True
    else:
        print(f"✗ Dashboard port is {dashboard_port}, expected 9120")
        return False


def test_funnel_routes():
    """Check current funnel routes."""
    print("\n=== Checking Funnel Routes ===")
    
    try:
        returncode, stdout, stderr = run_command(["tailscale", "funnel", "status"])
        
        if returncode != 0:
            print(f"Error getting funnel status: {stderr}")
            return False
        
        print("Current funnel routes:")
        for line in stdout.split('\n'):
            if 'proxy' in line and '8765' in line or '8767' in line:
                print(f"⚠️  Found route to old MCP port: {line.strip()}")
            elif 'proxy' in line and '9120' in line:
                print(f"✓ Found route to correct dashboard port: {line.strip()}")
        
        return True
        
    except Exception as e:
        print(f"Error checking funnel routes: {e}")
        return False


def main():
    """Run all regression tests."""
    print("Dashboard Port Drift Regression Test")
    print("=" * 50)
    
    results = []
    
    # Test 1: Port configuration
    results.append(("Port Configuration", test_port_configuration()))
    
    # Test 2: Funnel routes
    results.append(("Funnel Routes", test_funnel_routes()))
    
    # Test 3: Canary with wrong port
    results.append(("Canary with Wrong Port", test_canary_with_wrong_port()))
    
    # Test 4: Canary with correct port
    results.append(("Canary with Correct Port", test_canary_with_correct_port()))
    
    # Test 5: Canary with funnel URL
    results.append(("Canary with Funnel URL", test_canary_with_funnel_url()))
    
    # Summary
    print("\n" + "=" * 50)
    print("REGRESSION TEST SUMMARY")
    print("=" * 50)
    
    passed = 0
    total = len(results)
    
    for test_name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status}: {test_name}")
        if result:
            passed += 1
    
    print(f"\nResults: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 All regression tests passed!")
        return 0
    else:
        print("❌ Some regression tests failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())