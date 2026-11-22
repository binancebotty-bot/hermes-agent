"""W1i — provable, staging-only config apply guard.

The writer that corrupted the live config at 16:45:55Z is unidentified, so this
module assumes a concurrent writer that does NOT cooperate with our sidecar lock.
It serialises *our own* writers, refuses on pre-replace drift, makes the commit a
single atomic rename, and re-reads the target afterwards.

THE REMAINING WINDOW (exactly, in operations)
---------------------------------------------
After the pre-hash verification succeeds, the guard performs these five
filesystem operations before the target is committed:

    1. open the temp file in the target's directory (O_CREAT|O_EXCL),
    2. write the candidate bytes into that temp file,
    3. fsync the temp file's fd,
    4. close the temp file,
    5. os.replace(temp, target).

The residual TOCTOU window is precisely those five operations. An external
writer that ignores our lock can still mutate the target inside that interval;
our os.replace would then overwrite its write (last writer wins) and we would
not detect it, because our verification happened *before* the mutation. The
post-verify read catches any writer that changes the target AFTER our replace,
but cannot catch a write we have already overwritten. The guard narrows the
race to five operations; it cannot eliminate it without a cooperating writer
or a kernel-level lock held by all writers.

Usage is programmatic via apply_guard(); the recurrence monitor is available as
`python scripts/config_apply_guard.py monitor --config <cfg> --baseline <json>`.
Nothing here writes a live config unless a caller explicitly passes one; this
delivery was tested only against temp fixtures.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import yaml


# ------------------------------------------------------------------- errors

class GuardError(Exception):
    """Base class for every named failure. Carries a machine-readable code."""

    code = "GUARD_ERROR"

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.details = details
        for key, value in details.items():
            setattr(self, key, value)


class LockHeld(GuardError):
    code = "LOCK_HELD"


class StaleLockTimeout(GuardError):
    code = "STALE_LOCK_TIMEOUT"


class DriftDetected(GuardError):
    code = "DRIFT_DETECTED"

    def __init__(self, expected: str, actual: str):
        super().__init__(
            f"DRIFT_DETECTED expected_pre_hash={expected} actual_hash={actual}",
            expected=expected, actual=actual,
        )


class TempWriteFailed(GuardError):
    code = "TEMP_WRITE_FAILED"


class ReplaceFailed(GuardError):
    code = "REPLACE_FAILED"


class PostVerifyMismatch(GuardError):
    code = "POST_VERIFY_MISMATCH"

    def __init__(self, expected: str, actual: str):
        super().__init__(
            f"POST_VERIFY_MISMATCH candidate_hash={expected} target_hash={actual}",
            expected=expected, actual=actual,
        )


class BaselineError(GuardError):
    code = "BASELINE_RECORD_TAMPERED"


# --------------------------------------------------------- low-level hooks
# These are the only platform/IO touchpoints; tests monkeypatch them to inject
# faults without ever touching a live config.

def _read_bytes(path):
    return Path(path).read_bytes()


def _write_temp(config_path, tmp_path, candidate):
    with open(tmp_path, "wb") as fh:
        fh.write(candidate)
        fh.flush()
        os.fsync(fh.fileno())


def _replace(tmp_path, config_path):
    os.replace(tmp_path, config_path)


def _pid_alive(pid: int) -> bool:
    """Return True only if pid is a currently running process (no signalling)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # POSIX: signal 0 only probes, never terminates
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _lock_pid(lock_path: Path):
    try:
        parts = lock_path.read_text(encoding="utf-8").strip().split()
    except (OSError, UnicodeDecodeError):
        return None
    if not parts:
        return None
    try:
        return int(parts[0])
    except ValueError:
        return None


# ------------------------------------------------------------ sidecar lock

@contextmanager
def exclusive_lock(config_path, *, timeout: float = 5.0, _sleep=time.sleep):
    """Exclusive <config>.lock via O_CREAT|O_EXCL; breaks stale (dead-pid) locks.

    A lock owned by a live process raises LockHeld immediately. A lock whose
    recorded pid is dead (or unreadable) is stale and is removed before retry.
    The timeout bounds how long we churn through stale locks.
    """
    lock_path = Path(str(config_path) + ".lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pid = _lock_pid(lock_path)
            if pid is not None and _pid_alive(pid):
                raise LockHeld(f"lock held by live pid {pid}: {lock_path}", pid=pid,
                               lock_path=str(lock_path))
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass  # another caller cleared it; retry with O_EXCL
            if time.monotonic() >= deadline:
                raise StaleLockTimeout(
                    f"could not acquire {lock_path} within {timeout}s",
                    lock_path=str(lock_path), timeout=timeout)
            _sleep(0.01)
            continue
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(f"{os.getpid()} {time.time()}\n")
                fh.flush()
                os.fsync(fh.fileno())
            break

    try:
        yield lock_path
    finally:
        # Only unlink if the lock is still ours (never delete a foreign re-acquire).
        owner = _lock_pid(lock_path)
        if owner is None or owner == os.getpid():
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass


# ------------------------------------------------------------------- apply

def apply_guard(config, candidate: bytes, expected_pre_hash: str, *,
                lock_timeout: float = 5.0) -> dict:
    """Verify-then-apply with a serialised, atomic, post-verified commit.

    Returns a result dict on success; raises a GuardError subclass on every
    named failure. The target is only ever the fully old or fully new file.
    """
    config_path = Path(config)
    candidate = bytes(candidate)
    candidate_hash = hashlib.sha256(candidate).hexdigest()

    with exclusive_lock(config_path, timeout=lock_timeout):
        # 1. VERIFY: read current bytes and compare to the expected pre-hash.
        actual_pre = hashlib.sha256(_read_bytes(config_path)).hexdigest()
        if actual_pre != expected_pre_hash:
            raise DriftDetected(expected_pre_hash, actual_pre)

        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix=f".{config_path.name}.", suffix=".tmp",
                dir=str(config_path.parent),
            )
            os.close(fd)
        except OSError as exc:
            raise TempWriteFailed(str(exc)) from exc

        try:
            # 2. WRITE + FSYNC in the SAME directory, then one atomic replace.
            try:
                _write_temp(config_path, tmp_path, candidate)
            except OSError as exc:
                raise TempWriteFailed(str(exc)) from exc

            try:
                _replace(tmp_path, config_path)
                tmp_path = None  # renamed away; nothing left to clean
            except OSError as exc:
                raise ReplaceFailed(str(exc)) from exc

            # 3. POST-VERIFY: re-read the target and assert the candidate hash.
            actual_post = hashlib.sha256(_read_bytes(config_path)).hexdigest()
            if actual_post != candidate_hash:
                raise PostVerifyMismatch(candidate_hash, actual_post)
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except FileNotFoundError:
                    pass

    return {
        "status": "APPLIED",
        "pre_hash": actual_pre,
        "candidate_hash": candidate_hash,
        "post_hash": actual_post,
    }


# -------------------------------------------------------- recurrence monitor

_MISSING = object()


def _flatten(obj, prefix=""):
    """Flatten a YAML tree into dotted leaf paths (lists use numeric indices)."""
    flat = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            flat.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            flat.update(_flatten(value, f"{prefix}.{index}"))
    else:
        flat[prefix or "<root>"] = obj
    return flat


def record_baseline(config, baseline_path):
    """Record the trusted config bytes and their sha256 for later monitoring."""
    raw = Path(config).read_bytes()
    record = {
        "schema": 1,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "config_yaml": raw.decode("utf-8"),
    }
    Path(baseline_path).write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def monitor(config, baseline_path) -> dict:
    """Compare live config sha256 to a recorded baseline; name changed YAML paths."""
    record = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    baseline_text = record["config_yaml"]
    baseline_sha = record["sha256"]
    if hashlib.sha256(baseline_text.encode("utf-8")).hexdigest() != baseline_sha:
        raise BaselineError("baseline record sha256 does not match stored yaml",
                            code="BASELINE_RECORD_TAMPERED")

    raw = Path(config).read_bytes()
    live_sha = hashlib.sha256(raw).hexdigest()
    if live_sha == baseline_sha:
        return {"status": "MATCH", "sha256": live_sha, "changed_paths": []}

    try:
        base_flat = _flatten(yaml.safe_load(baseline_text))
        live_flat = _flatten(yaml.safe_load(raw.decode("utf-8")))
        changed = sorted(
            path for path in set(base_flat) | set(live_flat)
            if base_flat.get(path, _MISSING) != live_flat.get(path, _MISSING)
        )
    except (yaml.YAMLError, UnicodeDecodeError):
        changed = ["<yaml-parse-error>"]
    return {
        "status": "DRIFT_DETECTED",
        "baseline_sha256": baseline_sha,
        "live_sha256": live_sha,
        "changed_paths": changed,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_base = sub.add_parser("record-baseline", help="record a trusted config baseline")
    p_base.add_argument("--config", required=True)
    p_base.add_argument("--baseline", required=True)

    p_mon = sub.add_parser("monitor", help="compare live config against baseline")
    p_mon.add_argument("--config", required=True)
    p_mon.add_argument("--baseline", required=True)

    args = parser.parse_args(argv)

    if args.cmd == "record-baseline":
        rec = record_baseline(args.config, args.baseline)
        print(f"BASELINE_RECORDED sha256={rec['sha256']}")
        return 0

    try:
        res = monitor(args.config, args.baseline)
    except GuardError as exc:
        print(f"MONITOR ERROR {exc.code}: {exc}")
        return 3

    if res["status"] == "MATCH":
        print(f"MONITOR OK sha256={res['sha256']}")
        return 0

    print(f"MONITOR DRIFT_DETECTED baseline={res['baseline_sha256']} "
          f"live={res['live_sha256']}")
    for path in res["changed_paths"]:
        print(f"CHANGED_PATH {path}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
