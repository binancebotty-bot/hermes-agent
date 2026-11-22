"""Rehearsal hooks must require an explicit --rehearse flag.

(a) WAKE_DRIFT alone is inert; (b) WAKE_STOP_AFTER alone never aborts an apply;
(c) --rehearse against the real home refuses; (d) --rehearse in a fake home
still injects drift as before. Every case drives the real tool as a subprocess
against a disposable fake HOME; no live install, credential or agent is used.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_wake_activate import TOOL, build_home, result, run_tool, sha

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


def real_home():
    """Operator home independent of the HOME/USERPROFILE rehearsal redirect."""
    drive, part = os.environ.get("HOMEDRIVE"), os.environ.get("HOMEPATH")
    return Path(drive + part) if drive and part else Path.home()


# ---- (a) WAKE_DRIFT with no --rehearse is inert ----------------------------

@pytest.mark.parametrize("kind", ["source", "config"])
def test_wake_drift_env_without_rehearse_is_inert(tmp_path, kind):
    plain = build_home(tmp_path / "plain")
    drift = build_home(tmp_path / "drift")
    drift_emitter_before = (drift.src / "emitter.py").read_bytes()

    plain_proc = run_tool(plain, ["--apply"])
    drift.env["WAKE_DRIFT"] = kind
    drift_proc = run_tool(drift, ["--apply"])

    assert plain_proc.returncode == 0, plain_proc.stdout + plain_proc.stderr
    assert drift_proc.returncode == 0, drift_proc.stdout + drift_proc.stderr
    assert result(drift_proc.stdout)["flag_state"] == "ON"
    # Both configs legitimately embed their own absolute home path; normalize
    # only that path, then require the remaining bytes to match exactly.
    normalize = lambda fx: fx.config.read_bytes().replace(str(fx.home).encode(), b"<HOME>")
    assert normalize(drift) == normalize(plain), "WAKE_DRIFT alone changed config bytes"
    assert (drift.src / "emitter.py").read_bytes() == drift_emitter_before, \
        "WAKE_DRIFT alone changed source bytes"
    assert b"external" not in normalize(drift) + (drift.src / "emitter.py").read_bytes()


# ---- (b) WAKE_STOP_AFTER with no --rehearse does not abort ------------------

@pytest.mark.parametrize("phase", STOP_PHASES)
def test_wake_stop_after_env_without_rehearse_does_not_abort(tmp_path, phase):
    fx = build_home(tmp_path)
    fx.env["WAKE_STOP_AFTER"] = phase
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SIMULATED DEATH" not in proc.stdout, proc.stdout
    assert result(proc.stdout)["state"] == "applied"


# ---- (c) --rehearse against the real home refuses --------------------------

def test_rehearse_against_real_home_refuses_and_mutates_nothing(tmp_path):
    fx = build_home(tmp_path)
    pre = sha(fx.config)
    # Redirect every write target to the fake home while naming the real home
    # as --home: even a broken gate could not touch a live file.
    fx.env["WAKE_HERMES_HOME"] = str(fx.hermes)
    home = real_home()
    assert home.is_dir()

    proc = run_env(fx, ["--apply", "--rehearse", "--home", str(home)])
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "REHEARSE_REAL_HOME" in proc.stdout, proc.stdout
    assert sha(fx.config) == pre, "config was mutated by a refused rehearsal"
    assert not fx.dst.exists()
    assert not fx.record.exists()
    assert not list(fx.hermes.glob("config.yaml.wake-backup-*"))


# ---- (d) --rehearse in a fake home still injects drift ---------------------

@pytest.mark.parametrize("kind", ["source", "config"])
def test_rehearse_in_fake_home_still_injects_drift(tmp_path, kind):
    fx = build_home(tmp_path)
    pre = sha(fx.config)
    proc = run_env(fx, ["--apply", "--rehearse"], WAKE_DRIFT=kind)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "drift" in proc.stdout.lower(), proc.stdout
    assert sha(fx.config) == pre, "live config not restored after drift refusal"
    assert not fx.dst.exists(), "plugin left installed after drift refusal"
    rec = json.loads(fx.record.read_text(encoding="utf-8"))
    assert rec["state"] == "rolled_back"
