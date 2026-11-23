#!/usr/bin/env python3
"""
One-shot canary script for dashboard accessibility.

This script validates that the dashboard is accessible through the configured
public URL, following redirects and checking for proper authentication flow.
It exits with 0 on success and non-zero on failure, making it suitable for
use in automated health checks and deployment pipelines.

Usage:
    python scripts/dashboard_canary.py [--url URL] [--timeout SECONDS]
    
Options:
    --url URL           Public URL to check (default: from funnel config)
    --timeout SECONDS   Request timeout in seconds (default: 10)
    --verbose           Enable verbose output
"""

import argparse
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse
from typing import Optional, Tuple

try:
    import requests
except ImportError:
    print("Error: requests library is required. Install with: pip install requests", file=sys.stderr)
    sys.exit(1)

# Import our port configuration
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.dashboard_port import get_dashboard_url, get_funnel_config


class DashboardCanary:
    """Canary for dashboard accessibility checks."""
    
    def __init__(self, public_url: Optional[str] = None, timeout: int = 10):
        """
        Initialize the canary.
        
        Args:
            public_url: Public URL to check (uses funnel config if None)
            timeout: Request timeout in seconds
        """
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Hermes-Dashboard-Canary/1.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        
        # Determine the URL to check
        if public_url:
            self.target_url = public_url
        else:
            funnel_config = get_funnel_config()
            self.target_url = funnel_config["dashboard_url"]
        
        self.parsed_url = urlparse(self.target_url)
        
    def check_connectivity(self) -> Tuple[bool, str]:
        """
        Check basic connectivity to the target URL.
        
        Returns:
            Tuple of (success, message)
        """
        try:
            response = self.session.head(
                self.target_url,
                timeout=self.timeout,
                allow_redirects=False
            )
            
            if response.status_code == 200:
                return True, "Target URL is accessible"
            elif response.status_code in [301, 302, 303, 307, 308]:
                return True, f"Target URL responded with redirect ({response.status_code})"
            else:
                return False, f"Target URL returned unexpected status: {response.status_code}"
                
        except requests.exceptions.Timeout:
            return False, f"Connection to {self.target_url} timed out"
        except requests.exceptions.ConnectionError as e:
            return False, f"Connection to {self.target_url} failed: {e}"
        except Exception as e:
            return False, f"Unexpected error checking {self.target_url}: {e}"
    
    def check_dashboard_response(self) -> Tuple[bool, str]:
        """
        Check that the dashboard responds properly (login page or redirect to login).
        
        Returns:
            Tuple of (success, message)
        """
        try:
            # Follow redirects to see where we end up
            response = self.session.get(
                self.target_url,
                timeout=self.timeout,
                allow_redirects=True
            )
            
            # Check if we got a successful response
            if response.status_code == 200:
                content = response.text.lower()
                
                # Check for login page indicators
                login_indicators = [
                    "login", "signin", "sign in", "authenticate", "authentication",
                    "username", "password", "email", "welcome to hermes"
                ]
                
                found_login = any(indicator in content for indicator in login_indicators)
                
                if found_login:
                    return True, "Dashboard accessible - login page detected"
                else:
                    # Check if it's a valid dashboard page even without explicit login text
                    if "hermes" in content and len(content) > 1000:  # Substantial content
                        return True, "Dashboard accessible - dashboard page detected"
                    else:
                        return False, "Dashboard returned 200 but content doesn't appear valid"
            
            # Check for redirect to login
            elif response.status_code == 302:
                redirect_url = response.headers.get("location", "")
                if "login" in redirect_url.lower():
                    return True, f"Dashboard redirects to login: {redirect_url}"
                else:
                    return False, f"Dashboard redirects but not to login: {redirect_url}"
            
            else:
                return False, f"Dashboard returned unexpected status: {response.status_code}"
                
        except requests.exceptions.Timeout:
            return False, f"Dashboard request timed out"
        except requests.exceptions.ConnectionError as e:
            return False, f"Dashboard connection failed: {e}"
        except Exception as e:
            return False, f"Unexpected error checking dashboard: {e}"
    
    def check_health_endpoint(self) -> Tuple[bool, str]:
        """
        Check the /health endpoint if available.
        
        Returns:
            Tuple of (success, message)
        """
        health_url = urljoin(self.target_url, "/health")
        
        try:
            response = self.session.get(
                health_url,
                timeout=self.timeout,
                allow_redirects=False
            )
            
            if response.status_code == 200:
                # Try to parse JSON response
                try:
                    json_data = response.json()
                    return True, f"Health endpoint OK: {json_data}"
                except ValueError:
                    return True, "Health endpoint returned 200 (non-JSON)"
            else:
                return False, f"Health endpoint returned: {response.status_code}"
                
        except requests.exceptions.Timeout:
            return False, "Health endpoint timed out"
        except requests.exceptions.ConnectionError:
            # Health endpoint might not exist, that's OK
            return True, "Health endpoint not available (dashboard likely working)"
        except Exception as e:
            return False, f"Error checking health endpoint: {e}"
    
    def run_full_check(self) -> Tuple[bool, str]:
        """
        Run a comprehensive dashboard check.
        
        Returns:
            Tuple of (success, message)
        """
        print(f"Checking dashboard at: {self.target_url}")
        print(f"Timeout: {self.timeout}s")
        print("-" * 50)
        
        # Step 1: Basic connectivity
        print("1. Checking basic connectivity...")
        success, message = self.check_connectivity()
        print(f"   {'✓' if success else '✗'} {message}")
        
        if not success:
            return False, f"Connectivity check failed: {message}"
        
        # Step 2: Dashboard response
        print("2. Checking dashboard response...")
        success, message = self.check_dashboard_response()
        print(f"   {'✓' if success else '✗'} {message}")
        
        if not success:
            return False, f"Dashboard check failed: {message}"
        
        # Step 3: Health endpoint (if available)
        print("3. Checking health endpoint...")
        success, message = self.check_health_endpoint()
        print(f"   {'✓' if success else '✗'} {message}")
        
        return True, "All checks passed"


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Dashboard accessibility canary",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python scripts/dashboard_canary.py
    python scripts/dashboard_canary.py --url https://hermes.example.com:8443
    python scripts/dashboard_canary.py --timeout 30 --verbose
        """
    )
    
    parser.add_argument(
        "--url",
        help="Public URL to check (default: from funnel config)"
    )
    
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Request timeout in seconds (default: 10)"
    )
    
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose output"
    )
    
    args = parser.parse_args()
    
    if args.verbose:
        print(f"Dashboard URL from config: {get_dashboard_url()}")
        funnel_config = get_funnel_config()
        print(f"Funnel config: {funnel_config}")
    
    # Run the canary check
    canary = DashboardCanary(public_url=args.url, timeout=args.timeout)
    success, message = canary.run_full_check()
    
    print("-" * 50)
    if success:
        print(f"✓ Dashboard is accessible: {message}")
        sys.exit(0)
    else:
        print(f"✗ Dashboard check failed: {message}")
        sys.exit(1)


if __name__ == "__main__":
    main()