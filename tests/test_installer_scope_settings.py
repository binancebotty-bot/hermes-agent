"""Installer must write AND preserve the reviewed safety settings.

Regression this pins: a re-apply wrote only enabled/lane/paths/ack_horizon and
silently dropped `scope`, reopening the conversation flood that only a hand-
edited config had been holding back. Every test drives --apply/--rollback as a
subprocess against a disposable fake HOME; the real config and plugin tree are
never read for writing.

(a) absent scope -> reviewed scope written
(b) operator-modified scope -> preserved verbatim
(c) unknown keys under the entry survive
(d) hook_budget_seconds / python_executable established when missing
(e) post-apply settings dict is exactly the reviewed set
(f) rollback restores the config byte-identically
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

from test_wake_activate import LANE, build_home, run_tool  # noqa: E402

REVIEWED_SCOPE = {
    "require_task_id": True,
    "task_id_prefix_allow": ["cron:", "subagent-"],
    "task_id_exclude_prefixes": ["cron:8cff233194f6", "local-"],
}
REVIEWED_HOOK_BUDGET_SECONDS = 25
JOURNAL = "event_outbox.jsonl"


def _settings(fx):
    cfg = yaml.safe_load(fx.config.read_text(encoding="utf-8"))
    return cfg["plugins"]["entries"]["wake-emitter"]["settings"]


def _expected_settings(fx, python_executable):
    return {
        "enabled": True,
        "lane": LANE,
        "journal_path": str(fx.hermes / "state" / JOURNAL),
        "peer_lane_map_path": str(fx.hermes / "state" / "peer_lane_map.json"),
        "sender_path": str(fx.hermes / "scripts" / "pane_send_guarded.py"),
        "pane_read_path": str(fx.hermes / "scripts" / "pane_read.py"),
        "ack_horizon_seconds": 1800.0,
        "scope": REVIEWED_SCOPE,
        "hook_budget_seconds": REVIEWED_HOOK_BUDGET_SECONDS,
        "python_executable": python_executable,
    }


# (a) ----------------------------------------------------------------------

def test_apply_establishes_reviewed_scope_when_absent(tmp_path):
    fx = build_home(tmp_path)
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _settings(fx).get("scope") == REVIEWED_SCOPE


# (b) ----------------------------------------------------------------------

OPERATOR_CONFIG = """\
# operator config with a hand-reviewed scope
plugins:
  disabled:
    - some-plugin
  enabled:
    - lane-guard
  entries:
    wake-emitter:
      settings:
        scope:
          require_task_id: false
          session_ids:
            - operator-session
          task_id_prefix_allow:
            - 'manual:'
"""


def test_apply_preserves_operator_scope_verbatim(tmp_path):
    fx = build_home(tmp_path, config_text=OPERATOR_CONFIG)
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _settings(fx).get("scope") == {
        "require_task_id": False,
        "session_ids": ["operator-session"],
        "task_id_prefix_allow": ["manual:"],
    }


# (c) ----------------------------------------------------------------------

UNKNOWN_KEYS_CONFIG = """\
plugins:
  disabled:
    - some-plugin
  enabled:
    - lane-guard
  entries:
    wake-emitter:
      extra_entry_key: keep-entry
      config:
        legacy_key: keep-legacy
      settings:
        mystery_key: keep-me
        nested_unknown:
          a: 1
          b: two
"""


def test_apply_preserves_unknown_keys_under_entry(tmp_path):
    fx = build_home(tmp_path, config_text=UNKNOWN_KEYS_CONFIG)
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    cfg = yaml.safe_load(fx.config.read_text(encoding="utf-8"))
    entry = cfg["plugins"]["entries"]["wake-emitter"]
    assert entry.get("extra_entry_key") == "keep-entry"
    assert entry.get("config", {}).get("legacy_key") == "keep-legacy"
    settings = entry["settings"]
    assert settings.get("mystery_key") == "keep-me"
    assert settings.get("nested_unknown") == {"a": 1, "b": "two"}


# (d) ----------------------------------------------------------------------

def test_apply_establishes_budget_and_python_executable_when_missing(tmp_path):
    # No WAKE_PYTHON_EXECUTABLE: the tool runs under the Hermes venv, so the
    # installer must resolve its own interpreter as the reviewed default.
    fx = build_home(tmp_path)
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    settings = _settings(fx)
    assert settings.get("hook_budget_seconds") == REVIEWED_HOOK_BUDGET_SECONDS
    assert settings.get("python_executable") == sys.executable


PRESENT_CONFIG = """\
plugins:
  disabled:
    - some-plugin
  enabled:
    - lane-guard
  entries:
    wake-emitter:
      settings:
        hook_budget_seconds: 7
        python_executable: C:/operator/python.exe
"""


def test_apply_preserves_budget_and_python_executable_when_present(tmp_path):
    fx = build_home(tmp_path, config_text=PRESENT_CONFIG)
    fx.env["WAKE_PYTHON_EXECUTABLE"] = sys.executable  # must NOT override existing
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    settings = _settings(fx)
    assert settings.get("hook_budget_seconds") == 7
    assert settings.get("python_executable") == "C:/operator/python.exe"


# (e) ----------------------------------------------------------------------

def test_apply_settings_dict_is_exact_after_apply(tmp_path):
    fx = build_home(tmp_path)
    fx.env["WAKE_PYTHON_EXECUTABLE"] = sys.executable
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _settings(fx) == _expected_settings(fx, sys.executable)


# (f) ----------------------------------------------------------------------

def test_rollback_restores_config_byte_identically(tmp_path):
    fx = build_home(tmp_path)
    before_bytes = fx.config.read_bytes()
    before_hash = hashlib.sha256(before_bytes).hexdigest()
    fx.env["WAKE_PYTHON_EXECUTABLE"] = sys.executable

    applied = run_tool(fx, ["--apply"])
    assert applied.returncode == 0, applied.stdout + applied.stderr
    after_apply = hashlib.sha256(fx.config.read_bytes()).hexdigest()
    assert after_apply != before_hash, "apply must have changed the config"

    rolled = run_tool(fx, ["--rollback"])
    assert rolled.returncode == 0, rolled.stdout + rolled.stderr
    out = {}
    for line in reversed(rolled.stdout.splitlines()):
        if line.startswith("RESULT "):
            out = json.loads(line[len("RESULT "):])
            break
    assert out.get("config_hash_matches") is True
    assert fx.config.read_bytes() == before_bytes, "rollback must restore pre-apply bytes"
    assert hashlib.sha256(fx.config.read_bytes()).hexdigest() == before_hash
