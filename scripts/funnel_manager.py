#!/usr/bin/env python3
"""
Funnel route management script.

This script manages the tailscale funnel routes to ensure they point to the
correct dashboard port and makes dead routes explicit to prevent silent
failures when the dashboard port drifts.

Usage:
    python scripts/funnel_manager.py --status
    python scripts/funnel_manager.py --update-dashboard
    python scripts/funnel_manager.py --remove-dead-routes
    python scripts/funnel_manager.py --list-mcp-routes
"""

import subprocess
import sys
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    print("Error: requests library is required. Install with: pip install requests", file=sys.stderr)
    sys.exit(1)

# Import our port configuration
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.dashboard_port import get_dashboard_port, get_dashboard_url, get_funnel_config, is_mcp_port


class FunnelManager:
    """Manager for tailscale funnel routes."""
    
    def __init__(self):
        """Initialize the funnel manager."""
        self.funnel_config = get_funnel_config()
        
    def run_command(self, command: List[str]) -> Tuple[int, str, str]:
        """
        Run a shell command and return result.
        
        Args:
            command: Command to run as list
            
        Returns:
            Tuple of (return_code, stdout, stderr)
        """
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
    
    def get_funnel_status(self) -> Optional[Dict]:
        """
        Get current funnel status.
        
        Returns:
            Dictionary with funnel status or None if failed
        """
        returncode, stdout, stderr = self.run_command(["tailscale", "funnel", "status", "--json"])
        
        if returncode != 0:
            print(f"Error getting funnel status: {stderr}")
            return None
        
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            print(f"Error parsing funnel status JSON: {stdout}")
            return None
    
    def list_routes(self) -> List[Dict]:
        """
        List all current funnel routes.
        
        Returns:
            List of route dictionaries
        """
        status = self.get_funnel_status()
        if not status:
            return []
        
        routes = []
        for key, route_info in status.items():
            if isinstance(route_info, dict) and "proxy" in route_info:
                routes.append({
                    "url": key,
                    "target": route_info["proxy"],
                    "is_mcp_port": is_mcp_port(int(route_info["proxy"].split(":")[-1]))
                })
        
        return routes
    
    def update_dashboard_route(self) -> bool:
        """
        Update the funnel routes to point to the correct dashboard port.
        
        Returns:
            True if successful, False otherwise
        """
        dashboard_url = self.funnel_config["dashboard_url"]
        dashboard_port = self.funnel_config["dashboard_port"]
        
        print(f"Updating funnel routes to point to dashboard at: {dashboard_url}")
        print(f"Dashboard port: {dashboard_port}")
        
        # Update the HTTPS route
        https_url = f"https://hermes-webhook.tail851b82.ts.net:{self.funnel_config['https_port']}"
        command = ["tailscale", "funnel", "--bg", "--https", str(self.funnel_config['https_port']), dashboard_url]
        
        print(f"Running: {' '.join(command)}")
        returncode, stdout, stderr = self.run_command(command)
        
        if returncode != 0:
            print(f"Error updating HTTPS route: {stderr}")
            return False
        
        print(f"✓ Updated HTTPS route: {https_url} -> {dashboard_url}")
        
        # Note: We don't update the alt port (10000) as that should remain for MCP
        # This makes the route separation explicit
        
        return True
    
    def remove_dead_routes(self) -> bool:
        """
        Remove routes that point to dead MCP ports.
        
        Returns:
            True if successful, False otherwise
        """
        routes = self.list_routes()
        dead_routes = []
        
        for route in routes:
            if route["is_mcp_port"]:
                dead_routes.append(route)
        
        if not dead_routes:
            print("No dead MCP routes found")
            return True
        
        print("Found dead MCP routes:")
        for route in dead_routes:
            print(f"  - {route['url']} -> {route['target']}")
        
        # For safety, we don't automatically remove routes
        # Instead, we document them and provide instructions
        print("\n⚠️  Dead routes detected but not automatically removed.")
        print("These routes should be manually updated or documented as MCP-only.")
        
        return False
    
    def list_mcp_routes(self) -> List[Dict]:
        """
        List routes that point to MCP ports.
        
        Returns:
            List of MCP route dictionaries
        """
        routes = self.list_routes()
        return [route for route in routes if route["is_mcp_port"]]
    
    def validate_routes(self) -> Tuple[bool, List[str]]:
        """
        Validate that routes are pointing to correct targets.
        
        Returns:
            Tuple of (is_valid, warnings)
        """
        routes = self.list_routes()
        warnings = []
        is_valid = True
        
        dashboard_port = self.funnel_config["dashboard_port"]
        
        for route in routes:
            target_port = int(route["target"].split(":")[-1])
            
            if route["is_mcp_port"]:
                warnings.append(
                    f"MCP route: {route['url']} -> {route['target']} "
                    f"(port {target_port} is traditional MCP port)"
                )
            elif target_port != dashboard_port:
                warnings.append(
                    f"Dashboard route: {route['url']} -> {route['target']} "
                    f"(expected port {dashboard_port}, got {target_port})"
                )
                is_valid = False
        
        return is_valid, warnings
    
    def print_status(self):
        """Print current funnel status and validation."""
        print("=== Tailscale Funnel Status ===")
        
        routes = self.list_routes()
        print(f"Found {len(routes)} routes:")
        
        for route in routes:
            status = "🟢" if not route["is_mcp_port"] else "🔵"
            print(f"  {status} {route['url']} -> {route['target']}")
        
        # Validate routes
        is_valid, warnings = self.validate_routes()
        
        print("\n=== Route Validation ===")
        if is_valid:
            print("✓ All routes are correctly configured")
        else:
            print("✗ Route configuration issues found:")
        
        for warning in warnings:
            print(f"  ⚠️  {warning}")
        
        # Check if dashboard is accessible
        print("\n=== Dashboard Accessibility ===")
        try:
            import requests
            response = requests.get(self.funnel_config["dashboard_url"], timeout=5)
            if response.status_code == 200:
                print("✓ Dashboard is accessible")
            else:
                print(f"✗ Dashboard returned status: {response.status_code}")
        except Exception as e:
            print(f"✗ Cannot access dashboard: {e}")


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Manage tailscale funnel routes for dashboard access"
    )
    
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show current funnel status and validation"
    )
    
    parser.add_argument(
        "--update-dashboard",
        action="store_true",
        help="Update funnel routes to point to current dashboard port"
    )
    
    parser.add_argument(
        "--remove-dead-routes",
        action="store_true",
        help="Remove routes pointing to dead MCP ports"
    )
    
    parser.add_argument(
        "--list-mcp-routes",
        action="store_true",
        help="List routes that point to MCP ports"
    )
    
    args = parser.parse_args()
    
    manager = FunnelManager()
    
    if args.status:
        manager.print_status()
    
    elif args.update_dashboard:
        success = manager.update_dashboard_route()
        if success:
            print("\n✓ Dashboard routes updated successfully")
            manager.print_status()
        else:
            print("\n✗ Failed to update dashboard routes")
            sys.exit(1)
    
    elif args.remove_dead_routes:
        manager.remove_dead_routes()
    
    elif args.list_mcp_routes:
        mcp_routes = manager.list_mcp_routes()
        print(f"Found {len(mcp_routes)} MCP routes:")
        for route in mcp_routes:
            print(f"  - {route['url']} -> {route['target']}")
    
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()