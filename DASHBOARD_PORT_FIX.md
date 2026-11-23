# Dashboard Port Drift Fix - Implementation Summary

## Problem Description

The Hermes dashboard port silently drifted from old MCP endpoints (8765, 8767) to port 9120, but the tailscale funnel routes were not updated, causing public web access to fail with 502 errors while the dashboard itself remained healthy.

## Root Cause

1. **No single source of truth**: Dashboard port was configured in multiple places without coordination
2. **Silent failures**: Funnel routes pointing to dead ports returned 502 errors without clear indication of the problem
3. **No reconciliation mechanism**: No process to ensure funnel routes match the actual dashboard port

## Solution Implemented

### 1. Single Source of Truth for UI Port

**File**: `config/dashboard_port.py`

- Centralized configuration for dashboard port (9120)
- Functions to get dashboard URL, host, and funnel configuration
- Helper functions to identify MCP ports and validate routes

```python
# Key functions:
get_dashboard_port() -> int           # Returns 9120 (from config or default)
get_dashboard_url() -> str             # Returns "http://127.0.0.1:9120"
get_funnel_config() -> dict            # Returns funnel configuration
is_mcp_port(port: int) -> bool         # Identifies traditional MCP ports
```

### 2. One-Shot Canary Script

**File**: `scripts/dashboard_canary.py`

- Validates dashboard accessibility through public URLs
- Follows redirects and checks for login page responses
- Exits with appropriate codes for automation (0=success, non-zero=failure)
- Comprehensive checking: connectivity, dashboard response, health endpoint

**Usage**:
```bash
# Check dashboard accessibility
python scripts/dashboard_canary.py

# Check specific URL
python scripts/dashboard_canary.py --url https://hermes.example.com:8443

# With custom timeout
python scripts/dashboard_canary.py --timeout 30
```

### 3. Funnel Route Management

**File**: `scripts/funnel_manager.py`

- Lists current funnel routes and identifies dead MCP ports
- Updates funnel routes to point to correct dashboard port
- Validates route configuration and provides clear status reporting

**Usage**:
```bash
# Show current status and validation
python scripts/funnel_manager.py --status

# Update dashboard routes
python scripts/funnel_manager.py --update-dashboard

# List MCP routes (dead routes)
python scripts/funnel_manager.py --list-mcp-routes
```

### 4. Regression Testing

**File**: `scripts/test_dashboard_regression.py`

- Demonstrates the problem before fix
- Shows canary detecting dead ports
- Validates the fix after implementation
- Provides automated testing for future regressions

## Results

### Before Fix
```
Funnel routes:
- https://hermes-webhook.tail851b82.ts.net:8443 -> http://127.0.0.1:8765 ❌ (dead)
- https://hermes-webhook.tail851b82.ts.net:10000 -> http://127.0.0.1:8767 ❌ (dead)
```

### After Fix
```
Funnel routes:
- https://hermes-webhook.tail851b82.ts.net:8443 -> http://127.0.0.1:9120 ✅ (correct)
- https://hermes-webhook.tail851b82.ts.net:10000 -> http://127.0.0.1:8767 ⚠️ (MCP-only)
```

### Canary Test Results
```
✓ Canary with Wrong Port (8765): Correctly detects failure
✓ Canary with Correct Port (9120): Passes when dashboard is running
✓ Funnel URL: Works after route update
```

## Implementation Steps

1. **Create centralized port configuration** (`config/dashboard_port.py`)
2. **Implement canary script** (`scripts/dashboard_canary.py`)
3. **Create funnel management tool** (`scripts/funnel_manager.py`)
4. **Develop regression tests** (`scripts/test_dashboard_regression.py`)
5. **Update funnel routes** to point to correct port
6. **Document dead routes** as MCP-only

## Benefits

1. **Prevents silent failures**: Clear indication when routes are misconfigured
2. **Single source of truth**: Eliminates port drift issues
3. **Automated validation**: Canary script catches issues before users notice
4. **Clear documentation**: Dead routes are explicitly identified
5. **Regression testing**: Automated tests prevent future regressions

## Usage Examples

### Health Check in CI/CD
```bash
# Check dashboard accessibility after deployment
python scripts/dashboard_canary.py --timeout 30
if [ $? -eq 0 ]; then
    echo "Dashboard is accessible"
else
    echo "Dashboard check failed - investigate"
    exit 1
fi
```

### Funnel Route Maintenance
```bash
# Check funnel status
python scripts/funnel_manager.py --status

# Update routes if needed
python scripts/funnel_manager.py --update-dashboard

# List MCP routes for documentation
python scripts/funnel_manager.py --list-mcp-routes
```

### Regular Testing
```bash
# Run regression tests
python scripts/test_dashboard_regression.py
```

## Files Modified/Created

1. `config/dashboard_port.py` - New centralized port configuration
2. `scripts/dashboard_canary.py` - New accessibility canary script
3. `scripts/funnel_manager.py` - New funnel route management tool
4. `scripts/test_dashboard_regression.py` - New regression test suite

## Verification

The fix has been verified to:
- ✅ Correctly identify dead MCP ports (8765, 8767)
- ✅ Update funnel routes to point to correct dashboard port (9120)
- ✅ Validate dashboard accessibility through canary script
- ✅ Provide clear status reporting and documentation
- ✅ Prevent future port drift through centralized configuration

## Future Maintenance

1. **Monitor funnel routes**: Regularly run `python scripts/funnel_manager.py --status`
2. **Test accessibility**: Include canary checks in deployment pipelines
3. **Update configuration**: If dashboard port changes, update `config/dashboard_port.py`
4. **Document changes**: Keep MCP route documentation current

This solution addresses the core issue while providing tools for ongoing maintenance and preventing future regressions.