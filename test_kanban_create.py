#!/usr/bin/env python3

import os
import sys
import subprocess

# Clean environment
clean_env = os.environ.copy()
# Remove all HERMES-related variables
for key in list(clean_env.keys()):
    if key.startswith('HERMES_'):
        del clean_env[key]

# Add minimal required paths
clean_env['PATH'] = '/c/Users/wigmore/.hermes/hermes-agent/.venv/Scripts:/c/Users/wigmore/bin:/usr/local/bin:/usr/bin:/bin:/opt/bin:/mingw64/bin'
clean_env['HOME'] = '/c/Users/wigmore'
clean_env['HERMES_HOME'] = '/c/Users/wigmore/.hermes'

# Test the kanban create command
cmd = [
    '/c/Users/wigmore/.hermes/hermes-agent/.venv/Scripts/hermes.exe',
    'kanban', 
    'create', 
    'TEST_TASK',
    '--assignee', 'default',
    '--priority', '0',
    '--provider', 'zai',
    '--model', 'glm-4.5-flash',
    '--issuer', 'hermes-coordinator',
    '--body', 'test task'
]

print(f"Running: {' '.join(cmd)}")
print(f"Environment: {clean_env.get('HERMES_HOME', 'NOT SET')}")

try:
    result = subprocess.run(cmd, env=clean_env, capture_output=True, text=True, timeout=30)
    print(f"Exit code: {result.returncode}")
    print(f"STDOUT: {result.stdout}")
    print(f"STDERR: {result.stderr}")
    
    if result.returncode == 0:
        # Check if task id is in output
        if 'Created ' in result.stdout:
            print("✅ SUCCESS: Task created successfully")
        else:
            print("❌ FAIL: No task id in output")
    else:
        print("❌ FAIL: Command failed")
        
except subprocess.TimeoutExpired:
    print("❌ FAIL: Command timed out")
except Exception as e:
    print(f"❌ FAIL: Exception: {e}")