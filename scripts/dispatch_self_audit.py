"""Fail-visible dispatch inventory and proof of completion; does not mutate routing.

The host dispatcher must call record_unrouteable at the end of EVERY pass,
with rejected names obtained from its real route resolver.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Iterable, Sequence


def record_unrouteable(unrouted: Iterable[str | Path], health_path: Path, *, emit=print) -> int:
    """Count and report all envelopes rejected by the authoritative resolver."""
    bad = sorted(str(p) for p in unrouted)
    count = len(bad)
    emit(f"ALERT UNROUTEABLE {count} first={bad[0] if bad else '-'}")
    health_path = Path(health_path)
    health_path.parent.mkdir(parents=True, exist_ok=True)
    current = json.loads(health_path.read_text(encoding="utf-8")) if health_path.exists() else {}
    if not isinstance(current, dict):
        raise ValueError("dispatch_health.json must contain a JSON object")
    current.update({"unrouteable_count": count, "unrouteable_first": bad[0] if bad else None})
    fd, tmp = tempfile.mkstemp(prefix=health_path.name + ".", dir=str(health_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(current, out, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, health_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return count


def prove_claim(claim: str, command: Sequence[str]) -> dict:
    """Refuse unexecuted or failing proof; return command, exit, stdout, stderr."""
    if not claim.strip() or not command or not all(isinstance(arg, str) and arg for arg in command):
        raise ValueError("claim and executable argv are required; unsupported DONE")
    try:
        result = subprocess.run(list(command), capture_output=True, text=True, check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"claim proof was not completed: {exc}") from exc
    evidence = {"claim": claim, "command": list(command), "exit_code": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr}
    if result.returncode != 0:
        raise RuntimeError(f"claim proof failed: {evidence}")
    return evidence
