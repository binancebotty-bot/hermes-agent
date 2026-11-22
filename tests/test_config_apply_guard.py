"""W1i — provable config apply guard tests.

Every test runs against tmp_path fixtures. Nothing under ~/.hermes is read for
writing, no apply/deploy/restart happens, and no cron is installed.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

try:
    import config_apply_guard as guard
except Exception:  # module not delivered yet -> every test fails, not a collection error
    guard = None

OLD = b"plugins:\n  enabled:\n    - lane-guard\n"
NEW = b"plugins:\n  enabled:\n    - lane-guard\n    - wake-emitter\n"


def _require():
    if guard is None:
        pytest.fail("config_apply_guard module is not implemented", pytrace=False)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def fixture(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_bytes(OLD)
    return cfg


def _spawn_lock_holder(lock_path: Path):
    """A genuine other process that owns <config>.lock and stays alive."""
    code = (
        "import os,pathlib,time;"
        f"p=pathlib.Path({str(lock_path)!r});"
        "p.write_text(str(os.getpid()),encoding='utf-8');"
        "time.sleep(20)"
    )
    return subprocess.Popen([sys.executable, "-c", code])


def _wait_for_lock(lock_path: Path, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if lock_path.exists() and lock_path.read_text(encoding="utf-8").strip().isdigit():
            return
        time.sleep(0.02)
    raise AssertionError("lock holder never created the sidecar lock")


# ---------------------------------------------------------------- happy path

def test_happy_path_applies_and_releases_lock(fixture):
    _require()
    lock = Path(str(fixture) + ".lock")
    res = guard.apply_guard(fixture, NEW, sha(OLD))
    assert res["status"] == "APPLIED"
    assert res["pre_hash"] == sha(OLD)
    assert res["candidate_hash"] == sha(NEW)
    assert res["post_hash"] == sha(NEW)
    assert fixture.read_bytes() == NEW
    assert not lock.exists(), "sidecar lock leaked after success"
    assert list(fixture.parent.glob(".*.tmp")) == [], "temp file leaked"


# ------------------------------------------------- six injected fault paths

def test_fault_lock_held_by_another_process_refuses(fixture):
    """Fault 1: a live foreign holder must cause a named refusal, no write."""
    _require()
    lock = Path(str(fixture) + ".lock")
    holder = _spawn_lock_holder(lock)
    try:
        _wait_for_lock(lock)
        holder_pid = int(lock.read_text(encoding="utf-8").strip())
        with pytest.raises(guard.GuardError) as ei:
            guard.apply_guard(fixture, NEW, sha(OLD))
        assert ei.value.code == "LOCK_HELD"
        assert str(holder_pid) in str(ei.value)
        assert fixture.read_bytes() == OLD
        assert int(lock.read_text(encoding="utf-8").strip()) == holder_pid
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_fault_stale_lock_from_dead_pid_is_recovered(fixture):
    """Fault 2: a dead holder's lock is stale; guard breaks it and applies."""
    _require()
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_pid = proc.pid
    proc.wait(timeout=30)
    assert not guard._pid_alive(dead_pid), "test needs a genuinely dead pid"

    lock = Path(str(fixture) + ".lock")
    lock.write_text(f"{dead_pid}\n", encoding="utf-8")

    res = guard.apply_guard(fixture, NEW, sha(OLD))
    assert res["status"] == "APPLIED"
    assert fixture.read_bytes() == NEW
    assert not lock.exists(), "stale lock was not cleared"


def test_fault_drift_before_replace_refuses_with_both_hashes(fixture):
    """Fault 3: target changed since expected hash -> DRIFT_DETECTED, no write."""
    _require()
    drifted = OLD + b"# external writer\n"
    fixture.write_bytes(drifted)

    with pytest.raises(guard.DriftDetected) as ei:
        guard.apply_guard(fixture, NEW, sha(OLD))
    err = ei.value
    assert err.code == "DRIFT_DETECTED"
    assert err.expected == sha(OLD)
    assert err.actual == sha(drifted)
    assert fixture.read_bytes() == drifted
    assert not Path(str(fixture) + ".lock").exists()


def test_fault_temp_write_failure_leaves_target_fully_old(monkeypatch, fixture):
    """Fault 4: partial temp write then OSError -> OLD bytes, temp cleaned."""
    _require()

    def fail_write(config_path, tmp_path, candidate):
        with open(tmp_path, "wb") as fh:
            fh.write(b"partial-candidate")
            fh.flush()
        raise OSError("injected temp write failure")

    monkeypatch.setattr(guard, "_write_temp", fail_write)
    with pytest.raises(guard.GuardError) as ei:
        guard.apply_guard(fixture, NEW, sha(OLD))
    assert ei.value.code == "TEMP_WRITE_FAILED"
    assert fixture.read_bytes() == OLD
    assert list(fixture.parent.glob(".*.tmp")) == [], "partial temp leaked"
    assert not Path(str(fixture) + ".lock").exists()


def test_fault_replace_failure_leaves_target_fully_old(monkeypatch, fixture):
    """Fault 5: replace aborts mid-way -> OLD bytes, no partial target."""
    _require()

    def fail_replace(tmp_path, config_path):
        Path(tmp_path).write_bytes(b"half-written-temp")  # temp partial only
        raise OSError("injected replace failure")

    monkeypatch.setattr(guard, "_replace", fail_replace)
    with pytest.raises(guard.GuardError) as ei:
        guard.apply_guard(fixture, NEW, sha(OLD))
    assert ei.value.code == "REPLACE_FAILED"
    assert fixture.read_bytes() == OLD
    assert list(fixture.parent.glob(".*.tmp")) == [], "partial temp leaked"
    assert not Path(str(fixture) + ".lock").exists()


def test_fault_post_verify_mismatch_is_a_named_failure(monkeypatch, fixture):
    """Fault 6: monkeypatched post-read disagrees -> named failure, disk fully NEW."""
    _require()
    real_read = guard._read_bytes
    calls = {"n": 0}

    def read_stale_after_replace(path):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_read(path)  # pre-hash check sees the real OLD bytes
        return OLD  # simulated post-replace read of a stale/corrupt target

    monkeypatch.setattr(guard, "_read_bytes", read_stale_after_replace)
    with pytest.raises(guard.GuardError) as ei:
        guard.apply_guard(fixture, NEW, sha(OLD))
    err = ei.value
    assert err.code == "POST_VERIFY_MISMATCH"
    assert err.expected == sha(NEW)
    assert err.actual == sha(OLD)
    assert calls["n"] == 2
    assert fixture.read_bytes() == NEW, "post-fault bytes must be wholly new, never partial"
    assert not Path(str(fixture) + ".lock").exists()


# --------------------------------------------------------------- recurrence

def test_monitor_matches_baseline(fixture, tmp_path):
    _require()
    baseline = tmp_path / "baseline.json"
    guard.record_baseline(fixture, baseline)
    res = guard.monitor(fixture, baseline)
    assert res["status"] == "MATCH"
    assert res["changed_paths"] == []
    assert res["sha256"] == sha(OLD)


def test_monitor_names_changed_yaml_paths_and_exits_nonzero(fixture, tmp_path):
    _require()
    baseline = tmp_path / "baseline.json"
    guard.record_baseline(fixture, baseline)
    fixture.write_bytes(OLD + b"    - wake-emitter\n")

    res = guard.monitor(fixture, baseline)
    assert res["status"] == "DRIFT_DETECTED"
    assert res["live_sha256"] == sha(OLD + b"    - wake-emitter\n")
    assert any("plugins.enabled" in p for p in res["changed_paths"]), res

    cli = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "config_apply_guard.py"),
         "monitor", "--config", str(fixture), "--baseline", str(baseline)],
        capture_output=True, text=True, timeout=30,
    )
    assert cli.returncode == 2, cli.stdout + cli.stderr
    assert "DRIFT_DETECTED" in cli.stdout
    changed_lines = [ln for ln in cli.stdout.splitlines() if ln.startswith("CHANGED_PATH ")]
    assert changed_lines, cli.stdout
    assert any("plugins.enabled" in ln for ln in changed_lines), cli.stdout


def test_monitor_refuses_tampered_baseline_record(fixture, tmp_path):
    _require()
    baseline = tmp_path / "baseline.json"
    guard.record_baseline(fixture, baseline)
    record = json.loads(baseline.read_text(encoding="utf-8"))
    record["sha256"] = "0" * 64
    baseline.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(guard.GuardError) as ei:
        guard.monitor(fixture, baseline)
    assert "BASELINE" in ei.value.code


def test_monitor_cli_exits_zero_when_baseline_matches(fixture, tmp_path):
    _require()
    baseline = tmp_path / "baseline.json"
    guard.record_baseline(fixture, baseline)
    cli = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "config_apply_guard.py"),
         "monitor", "--config", str(fixture), "--baseline", str(baseline)],
        capture_output=True, text=True, timeout=30,
    )
    assert cli.returncode == 0, cli.stdout + cli.stderr
    assert "MONITOR OK" in cli.stdout


# ------------------------------------------------------------------- docs

def test_module_docstring_documents_residual_window_in_operations():
    _require()
    doc = guard.__doc__ or ""
    assert "os.replace" in doc
    assert "fsync" in doc.lower()
    assert "window" in doc.lower()
    assert "operations" in doc.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
