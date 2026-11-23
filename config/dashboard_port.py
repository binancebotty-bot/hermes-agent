"""
Single source of truth for the dashboard port configuration.

This module provides centralized configuration for the dashboard port to ensure
that both the funnel configuration and health checks use the same port value,
preventing the silent orphaning issue when the dashboard port drifts.
"""

import os
from pathlib import Path
from typing import Optional

# Default dashboard port - this should be the only place the default is defined
DEFAULT_DASHBOARD_PORT = 9120

# Default funnel ports for public access
DEFAULT_FUNNEL_HTTPS_PORT = 8443
DEFAULT_FUNNEL_HTTP_PORT = 80  # Not used by tailscale funnel
DEFAULT_FUNNEL_ALT_PORT = 10000

def get_dashboard_port() -> int:
    """
    Get the dashboard port from configuration or use default.
    
    Returns:
        The dashboard port number (default: 9120)
    """
    try:
        # Try to read from environment variable first
        port = os.environ.get("HERMES_DASHBOARD_PORT")
        if port:
            return int(port)
        
        # Try to read from config file
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly()
        webapp_config = config.get("webapp", {})
        
        if "port" in webapp_config:
            return int(webapp_config["port"])
            
    except (ValueError, OSError, ImportError):
        pass
    
    # Fallback to default
    return DEFAULT_DASHBOARD_PORT

def get_dashboard_host() -> str:
    """
    Get the dashboard host from configuration or use default.
    
    Returns:
        The dashboard host (default: "127.0.0.1")
    """
    try:
        # Try to read from config file
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly()
        webapp_config = config.get("webapp", {})
        
        if "host" in webapp_config:
            return webapp_config["host"]
            
    except (ValueError, OSError, ImportError):
        pass
    
    # Fallback to default
    return "127.0.0.1"

def get_dashboard_url() -> str:
    """
    Get the full dashboard URL.
    
    Returns:
        The dashboard URL (e.g., "http://127.0.0.1:9120")
    """
    host = get_dashboard_host()
    port = get_dashboard_port()
    
    if host in ["0.0.0.0", "::"]:
        # For wildcard binds, use localhost for health checks
        host = "127.0.0.1"
    
    return f"http://{host}:{port}"

def get_funnel_config() -> dict:
    """
    Get funnel configuration for public access.
    
    Returns:
        Dictionary with funnel configuration
    """
    return {
        "https_port": DEFAULT_FUNNEL_HTTPS_PORT,
        "alt_port": DEFAULT_FUNNEL_ALT_PORT,
        "dashboard_url": get_dashboard_url(),
        "dashboard_port": get_dashboard_port(),
    }

def is_mcp_port(port: int) -> bool:
    """
    Check if a port is traditionally used for MCP servers.
    
    Args:
        port: The port number to check
        
    Returns:
        True if the port is traditionally used for MCP
    """
    return port in {8765, 8767}  # Old MCP endpoints

def get_dashboard_health_check_url() -> str:
    """
    Get the URL for health checks.
    
    Returns:
        The health check URL
    """
    dashboard_url = get_dashboard_url()
    return f"{dashboard_url}/health"