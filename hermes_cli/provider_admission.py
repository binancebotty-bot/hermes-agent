"""Provider admission checking utilities for the dispatcher."""

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Provider model IDs that are permitted for probing
PERMITTED_PROBE_MODELS = {
    "gemini": "gemini/gemini-3.1-flash-lite",
    "zai": "zai/glm-4.5-flash", 
    "nous": "nous/stepfun/step-3.7-flash:free",
    "nous/meituan": "nous/meituan/longcat-2.5:free",
    "nous/upstage": "nous/upstage/solar-mini4:free"
}

# Forbidden providers (never probed)
FORBIDDEN_PROVIDERS = {"custom-deepseek-free"}

# TTL for admission records (30 minutes for free providers)
ADMISSION_TTL_SECONDS = 1800

def get_admission_file_path() -> Path:
    """Get the path to the provider admission state file."""
    home = Path.home()
    hermes_home = home / ".hermes"
    return hermes_home / "state" / "provider_admission.json"

def load_admission_state() -> Dict[str, Dict[str, Dict]]:
    """Load the provider admission state from disk."""
    admission_file = get_admission_file_path()
    if not admission_file.exists():
        return {}
    
    try:
        with open(admission_file, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

def save_admission_state(admission_state: Dict[str, Dict[str, Dict]]) -> None:
    """Save the provider admission state to disk."""
    admission_file = get_admission_file_path()
    admission_file.parent.mkdir(parents=True, exist_ok=True)
    
    with open(admission_file, 'w') as f:
        json.dump(admission_state, f, indent=2)

def is_provider_admitted(provider: str, model: str, admission_state: Optional[Dict] = None) -> Tuple[bool, Optional[str], Optional[int]]:
    """
    Check if a provider/model is currently admitted.
    
    Returns:
        Tuple of (is_admitted, reason, age_seconds)
    """
    if admission_state is None:
        admission_state = load_admission_state()
    
    provider_data = admission_state.get(provider, {})
    model_data = provider_data.get(model)
    
    if model_data is None:
        return False, "NOT_PROBED", None
    
    status = model_data.get("status")
    probed_at = model_data.get("probed_at")
    
    if status != "OK":
        return False, status, None
    
    if probed_at is None:
        return False, "STALE", None
    
    # Check TTL
    age = time.time() - probed_at
    if age > ADMISSION_TTL_SECONDS:
        return False, "STALE", int(age)
    
    return True, "OK", int(age)

def get_lane_admission_status(lane: str) -> Tuple[bool, Optional[str], Optional[int]]:
    """
    Check admission status for a lane (provider/model combination).
    
    Returns:
        Tuple of (is_admitted, reason, age_seconds)
    """
    if not lane:
        return True, "NO_LANE", None
    
    # Parse lane format (provider/model)
    if "/" in lane:
        provider, model = lane.split("/", 1)
    else:
        provider = lane
        model = None
    
    # Skip forbidden providers
    if provider in FORBIDDEN_PROVIDERS:
        return False, "FORBIDDEN", None
    
    # Check if this provider is permitted for probing
    if provider not in PERMITTED_PROBE_MODELS:
        return True, "PERMITTED_UNPROBED", None
    
    # If model is specified, check specific model admission
    if model:
        return is_provider_admitted(provider, model)
    else:
        # If no model specified, check if provider has any admitted models
        admission_state = load_admission_state()
        provider_data = admission_state.get(provider, {})
        
        for model_name, model_data in provider_data.items():
            if (model_data.get("status") == "OK" and 
                model_data.get("probed_at") and
                time.time() - model_data.get("probed_at", 0) <= ADMISSION_TTL_SECONDS):
                return True, "OK", int(time.time() - model_data.get("probed_at", 0))
        
        return False, "NO_ADMITTED_MODELS", None

def format_admission_status(lane: str) -> str:
    """Format admission status for display in diagnostics."""
    is_admitted, reason, age = get_lane_admission_status(lane)
    
    if is_admitted:
        if age:
            return f"OK ({age}s)"
        else:
            return "OK"
    else:
        return f"DOWN ({reason})"

def get_admission_diagnostics() -> List[str]:
    """Get formatted admission diagnostics for all known lanes."""
    diagnostics = []
    admission_state = load_admission_state()
    
    # Check all permitted providers
    for provider in PERMITTED_PROBE_MODELS:
        if provider in admission_state:
            provider_data = admission_state[provider]
            for model, model_data in provider_data.items():
                age = int(time.time() - model_data.get("probed_at", 0)) if model_data.get("probed_at") else None
                diagnostics.append(f"{provider}/{model}: {model_data.get('status', 'UNKNOWN')}{f' ({age}s)' if age else ''}")
        else:
            diagnostics.append(f"{provider}: NOT_PROBED")
    
    return diagnostics