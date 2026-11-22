"""Installer hardening: review findings H1-H5 + controller additions.

Every test drives the real tool (subprocess where bytes matter) against a
disposable fake HOME. No live install, no deploy, no credential, no agent.
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from test_wake_activate import TOOL, LANE, _git, build_home, result, run_tool, sha

STOP_PHASES = ["preconditions", "backup", "plugin", "config", "record"]


def run_env(fx, args, **overrides):
    env = dict(fx.env)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run([sys.executable, str(TOOL), *args], env=env,
                          capture_output=True, text=True, timeout=120)


def _load_tool(name):
    spec = importlib.util.spec_from_file_location(name, TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- H1: default source/repo must be the reviewed artefact ----------------

def test_h1_defaults_point_to_reviewed_artefact_and_refuse_bare_apply(tmp_path, monkeypatch):
    for key in ("WAKE_SOURCE", "WAKE_REPO"):
        monkeypatch.delenv(key, raising=False)
    tool = _load_tool("wake_activate_h1")
    paths = tool.build_paths(home=str(tmp_path))
    reviewed = Path(tmp_path) / "oc-runs" / "w1-activate-w1"
    assert paths.source == reviewed / "plugins" / "wake-emitter"
    assert paths.repo == reviewed

    # A bare apply with no SHA and no source/repo override must refuse, and must
    # not silently install the *other* tree that used to be the default.
    fx = build_home(tmp_path / "bare")
    pre = sha(fx.config)
    proc = run_env(fx, ["--apply"], WAKE_SOURCE=None, WAKE_REPO=None,
                   WAKE_EXPECTED_SHA=None)
    assert proc.returncode != 0
    assert "w1-activate-w1" in proc.stdout, proc.stdout
    assert sha(fx.config) == pre
    assert not fx.dst.exists()
    assert not fx.record.exists()


# ---- H2: mandatory live config SHA, checked before any write --------------

def test_h2_wrong_expected_config_sha_refuses_before_write(tmp_path):
    fx = build_home(tmp_path)
    pre = sha(fx.config)
    bad = "f" * 64
    fx.env["WAKE_EXPECTED_CONFIG_SHA"] = bad
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert bad in proc.stdout and pre in proc.stdout, proc.stdout
    assert sha(fx.config) == pre
    assert not fx.dst.exists()
    assert not fx.record.exists()
    assert not list(fx.hermes.glob("config.yaml.wake-backup-*"))


# ---- H3: rollback verifies the backup before overwriting the live config --

def test_h3_corrupt_backup_refuses_without_touching_live_config(tmp_path):
    fx = build_home(tmp_path)
    assert run_tool(fx, ["--apply"]).returncode == 0
    live_before = sha(fx.config)
    rec = json.loads(fx.record.read_text(encoding="utf-8"))
    backup = Path(rec["backup_path"])
    backup.write_text("CORRUPTED BACKUP", encoding="utf-8")
    corrupt_sha = sha(backup)

    proc = run_tool(fx, ["--rollback"])
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert sha(fx.config) == live_before, "live config was clobbered by a bad backup"
    assert corrupt_sha in proc.stdout and rec["config_sha256_before"] in proc.stdout
    assert fx.dst.exists(), "plugin removed despite refused rollback"
    assert json.loads(fx.record.read_text(encoding="utf-8"))["state"] == "applied"


# ---- H4: a PRE-RECORD makes a mid-apply death recoverable ------------------

def test_h4_pre_record_written_before_first_mutation_and_rollback_recovers(
        tmp_path, monkeypatch):
    fx = build_home(tmp_path)
    pre = sha(fx.config)
    monkeypatch.setenv("WAKE_EXPECTED_CONFIG_SHA", pre)
    tool = _load_tool("wake_activate_h4")
    paths = tool.build_paths(home=str(fx.home), source=str(fx.src),
                             repo=str(fx.repo), lane=LANE)

    real_write = tool.write_record
    calls = {"n": 0}

    def dying_write(path, record):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("simulated death before COMPLETE flip")
        return real_write(path, record)

    tool.write_record = dying_write
    try:
        with pytest.raises(RuntimeError, match="simulated death"):
            tool.apply(paths, fx.head)
    finally:
        tool.write_record = real_write

    rec = json.loads(fx.record.read_text(encoding="utf-8"))
    assert rec["state"] == "pre", "a PRE-RECORD must exist before the first mutation"
    assert rec["config_sha256_before"] == pre
    assert rec["backup_path"] and Path(rec["backup_path"]).is_file()
    assert rec["plugin_dir"] == str(fx.dst)
    assert fx.dst.exists(), "death must be simulated after the plugin copy"
    assert tool.read_flag(tool.load_config(fx.config))["flag_state"] == "ON"

    rolled = run_tool(fx, ["--rollback"])
    assert rolled.returncode == 0, rolled.stdout + rolled.stderr
    assert result(rolled.stdout)["verified"] is True
    assert sha(fx.config) == pre
    assert not fx.dst.exists()
    assert json.loads(fx.record.read_text(encoding="utf-8"))["state"] == "rolled_back"


# ---- H5: plugins destination writability is a precondition ----------------

def test_h5_unwritable_plugins_destination_refuses_before_write(tmp_path):
    fx = build_home(tmp_path)
    pre = sha(fx.config)
    plugins_dir = fx.hermes / "plugins"
    os.chmod(plugins_dir, stat.S_IREAD)
    try:
        proc = run_tool(fx, ["--apply"])
    finally:
        os.chmod(plugins_dir, stat.S_IWRITE | stat.S_IREAD)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "plugins destination not writable" in proc.stdout, proc.stdout
    assert sha(fx.config) == pre
    assert not fx.dst.exists()
    assert not fx.record.exists()


# ---- Controller (1): recovery verdict at every interruption step ----------

@pytest.mark.parametrize("phase", STOP_PHASES)
def test_interrupted_apply_recovery_at_each_step(tmp_path, phase):
    fx = build_home(tmp_path / phase)
    pre = sha(fx.config)
    # Crash injection is rehearsal-only; opt in explicitly now that the hook is gated.
    killed = run_env(fx, ["--apply", "--rehearse"], WAKE_STOP_AFTER=phase)
    assert killed.returncode == 3, killed.stdout + killed.stderr
    assert f"SIMULATED DEATH after {phase}" in killed.stdout, killed.stdout

    rolled = run_tool(fx, ["--rollback"])
    if phase == "preconditions":
        # Nothing was mutated; the correct outcome is a fail-closed refusal.
        assert rolled.returncode != 0
        assert sha(fx.config) == pre
        assert not fx.dst.exists()
        verdict = "NOTHING TO RECOVER (refuses: no activation record)"
    else:
        assert rolled.returncode == 0, rolled.stdout + rolled.stderr
        res = result(rolled.stdout)
        assert res["verified"] is True
        assert sha(fx.config) == pre
        assert not fx.dst.exists()
        assert json.loads(fx.record.read_text(encoding="utf-8"))["state"] == "rolled_back"
        verdict = "RECOVERED (config byte-identical, plugin removed)"
    operator_line = next((ln for ln in rolled.stdout.splitlines()
                          if ln.startswith(("REFUSED", "RESULT"))), "")
    print(f"VERDICT {phase}: {verdict} | operator sees: {operator_line}")


# ---- Controller (2): TOCTOU drift between check and write ------------------

@pytest.mark.parametrize("kind", ["config", "source"])
def test_toctou_drift_fails_closed(tmp_path, kind):
    fx = build_home(tmp_path)
    pre = sha(fx.config)
    # Drift injection is rehearsal-only; opt in explicitly now that the hook is gated.
    proc = run_env(fx, ["--apply", "--rehearse"], WAKE_DRIFT=kind)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "drift" in proc.stdout.lower(), proc.stdout
    assert sha(fx.config) == pre, "live config not restored after drift refusal"
    assert not fx.dst.exists(), "plugin left installed after drift refusal"
    rec = json.loads(fx.record.read_text(encoding="utf-8"))
    assert rec["state"] == "rolled_back"


# ---- Controller (3): no inert-but-green install ----------------------------

def test_post_install_verification_accepts_loadable_plugin(tmp_path):
    fx = build_home(tmp_path)
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    verification = result(proc.stdout)["verification"]
    assert verification["manifest_gate"] == "load"
    assert verification["lane"] == LANE
    assert verification["paths_absolute"] is True
    rec = json.loads(fx.record.read_text(encoding="utf-8"))
    assert rec["post_install_verification"]["manifest_gate"] == "load"

    # Independent check through the real discovery/gate API.
    sys.path.insert(0, str(Path(sys.prefix).parent))
    from hermes_cli.plugins_discovery import gate_manifest, manifest_key, scan_directory
    manifests = [m for m in scan_directory(fx.hermes / "plugins", "user")
                 if manifest_key(m) == "wake-emitter"]
    assert len(manifests) == 1
    assert gate_manifest(manifests[0], set(), {"wake-emitter"}).action == "load"


def test_post_install_verification_rejects_manifestless_plugin(tmp_path):
    fx = build_home(tmp_path)
    (fx.src / "plugin.yaml").unlink()
    _git(fx.repo, "add", "-A")
    _git(fx.repo, "commit", "-qm", "remove manifest for inert-install rehearsal")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=fx.repo,
                          capture_output=True, text=True, check=True).stdout.strip()
    fx.env["WAKE_EXPECTED_SHA"] = head
    pre = sha(fx.config)

    proc = run_tool(fx, ["--apply"])
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "manifest" in proc.stdout.lower(), proc.stdout
    assert sha(fx.config) == pre
    assert not fx.dst.exists()
    assert json.loads(fx.record.read_text(encoding="utf-8"))["state"] == "rolled_back"
