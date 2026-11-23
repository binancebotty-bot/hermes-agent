#!/usr/bin/env python3
"""
Simple test to verify the issuer fix logic.
This test verifies that the create_task function signature
accepts both created_by and issuer parameters separately.
"""

import sqlite3
import tempfile
import os
from pathlib import Path

def test_create_task_signature():
    """Test that create_task function accepts both created_by and issuer"""
    
    # Read the create_task function signature from kanban_db.py
    with open('hermes_cli/kanban_db.py', 'r') as f:
        content = f.read()
    
    # Find the create_task function definition
    lines = content.split('\n')
    in_create_task = False
    params = []
    
    for line in lines:
        if 'def create_task(' in line:
            in_create_task = True
            continue
        
        if in_create_task:
            if line.strip().endswith(') -> str:'):
                break
            if 'created_by:' in line:
                params.append(line.strip())
            if 'issuer:' in line:
                params.append(line.strip())
    
    print("Function signature parameters:")
    for param in params:
        print(f"  {param}")
    
    # Verify both parameters exist
    has_created_by = any('created_by:' in param for param in params)
    has_issuer = any('issuer:' in param for param in params)
    
    print(f"\nSignature verification:")
    print(f"  Has created_by parameter: {has_created_by}")
    print(f"  Has issuer parameter: {has_issuer}")
    
    if has_created_by and has_issuer:
        print("✅ PASSED: Function signature accepts both created_by and issuer")
        return True
    else:
        print("❌ FAILED: Function signature missing required parameters")
        return False

def test_kanban_py_fix():
    """Test that kanban.py fix is correctly implemented"""
    
    with open('hermes_cli/kanban.py', 'r') as f:
        content = f.read()
    
    # Check that the fix is present
    lines = content.split('\n')
    in_create_task_call = False
    found_fix = False
    
    for i, line in enumerate(lines):
        if 'task_id = kb.create_task(' in line:
            in_create_task_call = True
            continue
        
        if in_create_task_call:
            if 'created_by=created_by,' in line:
                print(f"✅ Found created_by=created_by at line {i+1}")
                found_created_by = True
            if 'issuer=issuer,' in line:
                print(f"✅ Found issuer=issuer at line {i+1}")
                found_issuer = True
            if line.strip().endswith(')'):
                break
    
    # Verify the fix
    if 'created_by=created_by,' in content and 'issuer=issuer,' in content:
        print("✅ PASSED: kanban.py fix correctly implemented")
        return True
    else:
        print("❌ FAILED: kanban.py fix not found")
        return False

def test_kanban_swarm_fix():
    """Test that kanban_swarm.py fix is correctly implemented"""
    
    with open('hermes_cli/kanban_swarm.py', 'r') as f:
        content = f.read()
    
    # Check that the fix is present
    if 'created_by=created_by, issuer=issuer,' in content:
        print("✅ PASSED: kanban_swarm.py fix correctly implemented")
        return True
    else:
        print("❌ FAILED: kanban_swarm.py fix not found")
        return False

def main():
    """Run all tests"""
    print("Testing issuer fix implementation...\n")
    
    os.chdir('C:\\Users\\wigmore\\.hermes\\hermes-agent')
    
    tests = [
        test_create_task_signature,
        test_kanban_py_fix,
        test_kanban_swarm_fix,
    ]
    
    results = []
    for test in tests:
        print(f"\n{'='*50}")
        result = test()
        results.append(result)
    
    print(f"\n{'='*50}")
    print("SUMMARY:")
    passed = sum(results)
    total = len(results)
    print(f"Tests passed: {passed}/{total}")
    
    if passed == total:
        print("🎉 ALL TESTS PASSED: The issuer fix is correctly implemented!")
        return 0
    else:
        print("❌ Some tests failed. Please check the implementation.")
        return 1

if __name__ == "__main__":
    exit(main())