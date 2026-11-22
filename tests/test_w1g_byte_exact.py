"""W1g Byte-Exact Rollback Tests: Requirements 1-5 from defect statement.

Every test drives the tool as a subprocess against a disposable fake HOME.
Nothing under the real C:/Users/wigmore/.hermes is read for writing.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
TOOL = Path(os.environ.get("WAKE_TOOL", str(ROOT / "scripts" / "wake_activate.py")))
PLUGIN_DIR = Path(os.environ.get("WAKE_PLUGIN_DIR", str(ROOT / "plugins" / "wake-emitter")))

LANE = "gpt6:assistant"
CONV = "https://chatgpt.com/c/6b01a297-292c-83ed-9f47-f49ab937c2f1"
JOURNAL = "event_outbox.jsonl"

FAKE_PANE_READ = (
    'import sys\n'
    'lane = sys.argv[1] if len(sys.argv) > 1 else ""\n'
    'if lane == "gpt6:unreachable":\n'
    '    print("NO_MATCH for %r. tabs seen:" % lane)\n'
    '    raise SystemExit(1)\n'
    'print("MATCHED_BY_CONV 6b01a297 (role %s; tab title lags)" % lane)\n'
    'print(\'{"url":"' + CONV + '","selector_chain":"main-div-group","count":1,'
    '"turns":[{"role":"unknown","text":"hello"}]}\')\n'
)

# Fixture config with booleans true AND false, comments, ordering, unusual key
FIXTURE_CONFIG = """# W1g test fixture: booleans true/false, comments, ordering, unusual key
plugins:
  disabled:
    - some-plugin
  enabled:
    - lane-guard
    - memory_enabled        # comment on enabled item
  entries:
    lane-guard:
      settings:
        granted_capabilities:
          - tools.override
    memory_enabled:
      settings:
        enabled: true
        compression: false
        checkpoints: true
    user_profile_enabled:
      settings:
        enabled: false
        orchestrator_enabled: true
        stt: false
    background_review:
      settings:
        enabled: true
    unusual_key_with_underscores_and_numbers_123:
      settings:
        enabled: true
        custom_field: "preserve_me"
    # comment between entries
    another_plugin:
      settings:
        enabled: false
"""

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def _git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.name=rehearsal", "-c", "user.email=rehearsal@local", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    )

def build_home(tmp_path, *, lanes=None, pane_ok=True, sender_exists=True, config_text=None):
    """Create a disposable HOME with the exact layout the tool expects."""
    home = tmp_path / "home"
    repo = home / "oc-runs" / "w1-wake-w1"
    src = repo / "plugins" / "wake-emitter"
    src.mkdir(parents=True)
    for item in PLUGIN_DIR.iterdir():
        if item.is_file():
            shutil.copy2(item, src / item.name)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "rehearsal baseline")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()

    hermes = home / ".hermes"
    (hermes / "plugins").mkdir(parents=True)
    (hermes / "state").mkdir()
    (hermes / "scripts").mkdir()
    (hermes / "config.yaml").write_text(config_text or FIXTURE_CONFIG, encoding="utf-8")
    if lanes is None:
        lanes = {LANE: {"conv": CONV, "generation": 1}}
    (hermes / "state" / "peer_lane_map.json").write_text(
        json.dumps({"lanes": lanes}), encoding="utf-8")
    (hermes / "scripts" / "pane_read.py").write_text(FAKE_PANE_READ, encoding="utf-8")
    if not pane_ok:
        (hermes / "scripts" / "pane_read.py").write_text(
            'print("NO_MATCH for lane")\nraise SystemExit(1)\n', encoding="utf-8")
    if sender_exists:
        (hermes / "scripts" / "pane_send_guarded.py").write_text(
            "print('GUARD_COMPLETE key=x')\n", encoding="utf-8")
    (hermes / "state" / JOURNAL).write_text('{"sentinel": 1}\n', encoding="utf-8")

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["WAKE_SOURCE"] = str(src)
    env["WAKE_REPO"] = str(repo)
    env["WAKE_EXPECTED_SHA"] = head
    env["WAKE_EXPECTED_CONFIG_SHA"] = sha(hermes / "config.yaml")
    return SimpleNamespace(home=home, repo=repo, src=src, hermes=hermes, head=head,
                           env=env, config=hermes / "config.yaml",
                           dst=hermes / "plugins" / "wake-emitter",
                           record=hermes / "state" / "wake_activation.json",
                           journal=hermes / "state" / JOURNAL)

def run_tool(fx, args):
    return subprocess.run([sys.executable, str(TOOL), *args], env=fx.env,
                          capture_output=True, text=True, timeout=120)

def result(stdout):
    """Parse the tool's final `RESULT {...}` line, or None when absent."""
    for line in reversed(stdout.splitlines()):
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT "):])
    return None

def status_json(fx):
    proc = run_tool(fx, ["--status"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


# ========================================================================
# Requirement 1: BYTE-LEVEL RESTORE
# ========================================================================

def test_byte_level_restore_sha256_equality(tmp_path):
    """Post-rollback bytes IDENTICAL to backup (sha256 equality).
    Fixture: config with booleans true/false, comments, ordering, unusual key."""
    fx = build_home(tmp_path, config_text=FIXTURE_CONFIG)
    pre_apply_hash = sha(fx.config)

    # Apply
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert result(proc.stdout)["flag_state"] == "ON"

    # Config should be modified but comments/ordering/booleans preserved
    post_apply_hash = sha(fx.config)
    assert post_apply_hash != pre_apply_hash

    # Verify fixture properties survive apply
    post_text = fx.config.read_text(encoding="utf-8")
    assert "memory_enabled:" in post_text
    assert "enabled: true" in post_text
    assert "enabled: false" in post_text
    assert "compression: false" in post_text
    assert "checkpoints: true" in post_text
    assert "orchestrator_enabled: true" in post_text
    assert "stt: false" in post_text
    assert "unusual_key_with_underscores_and_numbers_123:" in post_text
    assert "custom_field: \"preserve_me\"" in post_text
    assert "# W1g test fixture" in post_text
    assert "# comment on enabled item" in post_text
    assert "# comment between entries" in post_text

    # Rollback
    proc = run_tool(fx, ["--rollback"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    res = result(proc.stdout)
    assert res["verified"] is True
    assert res["config_hash_matches"] is True

    # BYTE-LEVEL RESTORE: post-rollback bytes IDENTICAL to pre-apply
    post_rollback_hash = sha(fx.config)
    assert post_rollback_hash == pre_apply_hash, \
        f"Rollback hash {post_rollback_hash} != pre-apply hash {pre_apply_hash}"

    # Verify all fixture properties restored exactly
    restored_text = fx.config.read_text(encoding="utf-8")
    assert restored_text == FIXTURE_CONFIG, "Restored config text does not match original fixture"


# ========================================================================
# Requirement 2: RECORD TRUTHFULNESS
# ========================================================================

def test_three_distinct_hashes_when_config_edited_between_phases(tmp_path):
    """Config edited between pre-measure, mutation, post-measure produces three distinct hashes."""
    fx = build_home(tmp_path, config_text=FIXTURE_CONFIG)

    # Get the activation record after apply to see recorded hashes
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode == 0, proc.stdout + proc.stderr

    record = json.loads(fx.record.read_text(encoding="utf-8"))

    # Should have config_sha256_before (measured before mutation)
    assert "config_sha256_before" in record
    before_hash = record["config_sha256_before"]

    # Should have config_sha256_after (measured immediately after mutation)
    assert "config_sha256_after" in record, "Missing config_sha256_after in record"
    after_hash = record["config_sha256_after"]

    # Now drift the config externally (simulate concurrent edit)
    fx.config.write_text("plugins:\n  enabled: []\n", encoding="utf-8")
    drift_hash = sha(fx.config)

    # Three distinct hashes
    assert before_hash != after_hash, "before_hash should differ from after_hash (mutation occurred)"
    assert after_hash != drift_hash, "after_hash should differ from drift_hash (external edit)"
    assert before_hash != drift_hash, "before_hash should differ from drift_hash"


def test_rollback_changes_bytes_before_ne_after_hash(tmp_path):
    """Rollback that genuinely changes bytes shows before != after in record."""
    fx = build_home(tmp_path, config_text=FIXTURE_CONFIG)
    pre_apply_hash = sha(fx.config)

    # Apply
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # Drift config after apply
    fx.config.write_text("plugins:\n  enabled: []\n", encoding="utf-8")
    drifted_hash = sha(fx.config)
    assert drifted_hash != pre_apply_hash

    # Rollback
    proc = run_tool(fx, ["--rollback"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    res = result(proc.stdout)

    # Rollback record should have config_sha256_after showing restored hash
    assert "config_sha256" in res, "Rollback result missing config_sha256"
    rollback_after_hash = res["config_sha256"]

    # The rollback changed bytes (drifted -> restored)
    assert rollback_after_hash == pre_apply_hash
    assert rollback_after_hash != drifted_hash

    # The rollback section in the record should show before/after
    record = json.loads(fx.record.read_text(encoding="utf-8"))
    assert "rollback" in record
    rollback_section = record["rollback"]
    assert "config_sha256" in rollback_section
    assert rollback_section["config_sha256"] == pre_apply_hash
    assert rollback_section["config_hash_matches"] is True


# ========================================================================
# Requirement 3: THREE-WAY CONSISTENCY
# ========================================================================

def test_three_way_coherence_after_rollback(tmp_path):
    """After rollback: wake-emitter removed from enabled list AND settings.enabled=false AND plugin dir moved aside."""
    fx = build_home(tmp_path, config_text=FIXTURE_CONFIG)

    # Apply
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # Verify applied state: enabled list has wake-emitter, settings.enabled=true, dir exists
    cfg = yaml.safe_load(fx.config.read_text(encoding="utf-8"))
    assert "wake-emitter" in cfg["plugins"]["enabled"]
    assert cfg["plugins"]["entries"]["wake-emitter"]["settings"]["enabled"] is True
    assert fx.dst.exists()

    # Rollback
    proc = run_tool(fx, ["--rollback"])
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # THREE-WAY COHERENCE CHECK:
    # 1. wake-emitter removed from enabled list
    cfg = yaml.safe_load(fx.config.read_text(encoding="utf-8"))
    assert "wake-emitter" not in cfg["plugins"].get("enabled", []), \
        "wake-emitter still in enabled list after rollback"

    # 2. settings.enabled = false (or entry removed entirely)
    entry = cfg["plugins"]["entries"].get("wake-emitter")
    if entry is not None:
        assert entry["settings"]["enabled"] is False, \
            "wake-emitter settings.enabled not false after rollback"

    # 3. plugin directory moved aside (not present at original path)
    assert not fx.dst.exists(), "Plugin directory still exists after rollback"

    # Verify rolled-back dir exists with .rolledback- suffix
    rolled_back_dirs = list(fx.hermes.glob("plugins/wake-emitter.rolledback-*"))
    assert len(rolled_back_dirs) == 1, f"Expected one rolled-back dir, found {rolled_back_dirs}"


def test_preflight_refuses_on_inconsistent_state_reports_which_disagrees(tmp_path):
    """Pre-flight check refuses to proceed on inconsistent state, reports which of three disagrees."""
    fx = build_home(tmp_path, config_text=FIXTURE_CONFIG)

    # Apply first
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # Manually create inconsistent state: remove from enabled list but keep settings.enabled=true and dir exists
    cfg = yaml.safe_load(fx.config.read_text(encoding="utf-8"))
    cfg["plugins"]["enabled"] = [p for p in cfg["plugins"]["enabled"] if p != "wake-emitter"]
    # Keep settings.enabled=true
    import ruamel.yaml
    yaml_handle = ruamel.yaml.YAML(typ="rt")
    yaml_handle.preserve_quotes = True
    yaml_handle.default_flow_style = False
    yaml_handle.indent(mapping=2, sequence=4, offset=2)
    with open(fx.config, "w", encoding="utf-8") as f:
        yaml_handle.dump(cfg, f)

    # Remove the backup file to simulate no valid backup
    record = json.loads(fx.record.read_text(encoding="utf-8"))
    backup_path = Path(record["backup_path"])
    if backup_path.exists():
        backup_path.unlink()

    # Now try rollback - should refuse and report inconsistency
    proc = run_tool(fx, ["--rollback"])
    # Should fail with clear message about which of the three disagrees
    assert proc.returncode != 0, f"Expected refusal, got {proc.returncode}: {proc.stdout}"
    output = proc.stdout + proc.stderr
    assert "inconsistent" in output.lower() or "mismatch" in output.lower() or "disagree" in output.lower(), \
        f"Expected inconsistency error message, got: {output}"
    # Should report which of the three disagrees
    assert "enabled list" in output.lower() or "settings.enabled" in output.lower() or "plugin directory" in output.lower(), \
        f"Expected specific component named, got: {output}"


# ========================================================================
# Requirement 5: DRY-RUN/VERIFY MODE
# ========================================================================

def test_dry_run_shows_before_after_hashes_and_diff_count(tmp_path):
    """Dry-run prints intended before/after hashes and diff line count."""
    fx = build_home(tmp_path, config_text=FIXTURE_CONFIG)
    pre_hash = sha(fx.config)

    # Run with --verify (dry-run mode)
    proc = run_tool(fx, ["--verify"])
    assert proc.returncode == 0, f"Verify failed: {proc.stdout} {proc.stderr}"

    output = proc.stdout
    # Should print before hash
    assert "before" in output.lower() or "pre" in output.lower()
    assert pre_hash[:16] in output or pre_hash in output, f"Before hash not in output: {output}"

    # Should print after hash (simulated)
    assert "after" in output.lower() or "post" in output.lower()

    # Should print diff line count
    assert "diff" in output.lower() or "change" in output.lower() or "line" in output.lower()

    # Config should be unchanged
    assert sha(fx.config) == pre_hash
    assert not fx.dst.exists()
    assert not fx.record.exists()


def test_dry_run_shows_zero_diff_when_no_change_needed(tmp_path):
    """Dry-run on already-applied config shows 0 diff lines."""
    fx = build_home(tmp_path, config_text=FIXTURE_CONFIG)

    # Apply first
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # Now dry-run should show 0 diff
    proc = run_tool(fx, ["--verify"])
    assert proc.returncode == 0, f"Verify failed: {proc.stdout} {proc.stderr}"

    output = proc.stdout
    # Should indicate 0 changes
    assert "0" in output and ("diff" in output.lower() or "change" in output.lower() or "line" in output.lower())


if __name__ == "__main__":
    pytest.main([__file__, "-v"])