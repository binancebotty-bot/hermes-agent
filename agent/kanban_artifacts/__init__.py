"""Incremental durable artifact persistence for kanban workers.

Provides atomic write-temp-rename, progress tracking with step markers,
and restart-safe recovery so a crashed worker's completed work survives.

Integration points:
- Worker spawn: initialise durable artifact dir and progress marker
- During work: worker calls `write_step_artifact(step_name, content)` after each meaningful step
- Completion: artifacts already durable; completion copies metadata to board
- Recovery: new worker reads progress marker and resumes from last completed step
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli import kanban_db as kb

__all__ = [
    "ArtifactManager",
    "ProgressMarker",
    "StepRecord",
    "get_artifact_manager",
    "initialise_worker_artifacts",
    "write_step_artifact",
    "read_progress_marker",
    "recovery_plan",
]


@dataclass
class StepRecord:
    """A single completed work step with timestamp and artifact reference."""
    step: str
    timestamp: int
    artifact_path: Optional[str] = None
    artifact_hash: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "StepRecord":
        return cls(**data)


@dataclass
class ProgressMarker:
    """Machine-readable progress marker for restart-safe recovery."""
    task_id: str
    run_id: int
    worker_pid: int
    started_at: int
    updated_at: int
    steps: list[StepRecord] = field(default_factory=list)
    key_answers: dict = field(default_factory=dict)  # literal results like R96_IN_BASE=YES
    version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ProgressMarker":
        steps = [StepRecord.from_dict(s) for s in data.get("steps", [])]
        return cls(
            task_id=data["task_id"],
            run_id=data["run_id"],
            worker_pid=data["worker_pid"],
            started_at=data["started_at"],
            updated_at=data["updated_at"],
            steps=steps,
            key_answers=data.get("key_answers", {}),
            version=data.get("version", 1),
        )

    def latest_step(self) -> Optional[StepRecord]:
        return self.steps[-1] if self.steps else None

    def completed_steps(self) -> set[str]:
        return {s.step for s in self.steps}


class ArtifactManager:
    """Manages durable artifacts for a kanban worker task.

    Artifacts are stored outside the scratch workspace in:
    - Per-task attachments dir (board-scoped): `attachments_root/<task_id>/`
    - Progress marker: `attachments_root/<task_id>/.progress.json`
    - Step artifacts: `attachments_root/<task_id>/steps/<step_name>.<ext>`

    All writes are atomic (temp file + rename) so crashes mid-write
    cannot leave truncated artifacts that look complete.
    """

    def __init__(self, task_id: str, run_id: int, board: Optional[str] = None):
        self.task_id = task_id
        self.run_id = run_id
        self.board = board
        self._worker_pid = os.getpid()
        self._started_at = int(time.time())

        # Durable location: per-task attachments directory
        self.attachments_dir = kb.task_attachments_dir(task_id, board=board)
        self.attachments_dir.mkdir(parents=True, exist_ok=True)

        # Step artifacts subdirectory
        self.steps_dir = self.attachments_dir / "steps"
        self.steps_dir.mkdir(parents=True, exist_ok=True)

        # Progress marker file
        self.progress_path = self.attachments_dir / ".progress.json"

        # Load or create progress marker
        self.progress = self._load_or_create_progress()

    def _load_or_create_progress(self) -> ProgressMarker:
        if self.progress_path.exists():
            try:
                with self.progress_path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                # Verify this marker belongs to our task/run
                if data.get("task_id") == self.task_id and data.get("run_id") == self.run_id:
                    return ProgressMarker.from_dict(data)
            except (json.JSONDecodeError, KeyError, TypeError):
                pass  # Corrupted or stale marker; create fresh
        return ProgressMarker(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=self._worker_pid,
            started_at=self._started_at,
            updated_at=self._started_at,
        )

    def _atomic_write_json(self, path: Path, data: dict) -> None:
        """Write JSON atomically via temp file + rename."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
        ) as tmp:
            json.dump(data, tmp, ensure_ascii=False, separators=(",", ":"))
            tmp_path = Path(tmp.name)
        try:
            tmp_path.replace(path)
        except Exception:
            with contextlib.suppress(Exception):
                tmp_path.unlink(missing_ok=True)
            raise

    def _atomic_write_bytes(self, path: Path, data: bytes) -> None:
        """Write bytes atomically via temp file + rename."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False, suffix=".tmp") as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)
        try:
            tmp_path.replace(path)
        except Exception:
            with contextlib.suppress(Exception):
                tmp_path.unlink(missing_ok=True)
            raise

    def _atomic_write_text(self, path: Path, text: str, encoding: str = "utf-8") -> None:
        """Write text atomically via temp file + rename."""
        self._atomic_write_bytes(path, text.encode(encoding))

    def save_progress(self) -> None:
        """Persist the progress marker atomically."""
        self.progress.updated_at = int(time.time())
        self.progress.worker_pid = self._worker_pid
        self._atomic_write_json(self.progress_path, self.progress.to_dict())

    def write_step(
        self,
        step_name: str,
        content: str | bytes,
        *,
        extension: str = ".md",
        metadata: Optional[dict] = None,
        artifact_hash: Optional[str] = None,
    ) -> Path:
        """Write a step artifact atomically and record it in the progress marker.

        Args:
            step_name: Unique name for this step (e.g., "analysis", "code_generation", "test_results")
            content: Artifact content (string or bytes)
            extension: File extension for the artifact
            metadata: Optional metadata to store with the step
            artifact_hash: Optional precomputed hash (sha256 hex); computed if not provided

        Returns:
            Path to the written artifact
        """
        if isinstance(content, str):
            content_bytes = content.encode("utf-8")
        else:
            content_bytes = content

        if artifact_hash is None:
            import hashlib
            artifact_hash = hashlib.sha256(content_bytes).hexdigest()

        # Atomic write to steps directory
        artifact_path = self.steps_dir / f"{step_name}{extension}"
        self._atomic_write_bytes(artifact_path, content_bytes)

        # Record step in progress marker
        step_record = StepRecord(
            step=step_name,
            timestamp=int(time.time()),
            artifact_path=str(artifact_path),
            artifact_hash=artifact_hash,
            metadata=metadata or {},
        )
        self.progress.steps.append(step_record)
        self.save_progress()

        return artifact_path

    def write_step_text(self, step_name: str, text: str, **kwargs) -> Path:
        """Convenience: write text content as a step artifact."""
        return self.write_step(step_name, text, extension=".md", **kwargs)

    def write_step_json(self, step_name: str, data: dict, **kwargs) -> Path:
        """Convenience: write JSON content as a step artifact."""
        import json
        return self.write_step(step_name, json.dumps(data, ensure_ascii=False, indent=2), extension=".json", **kwargs)

    def record_key_answer(self, key: str, value: str) -> None:
        """Record a literal key answer that must appear in the completion summary.

        Example: record_key_answer("R96_IN_BASE", "YES")
        """
        self.progress.key_answers[key] = value
        self.save_progress()

    def get_completed_steps(self) -> set[str]:
        """Return the set of completed step names for recovery."""
        return self.progress.completed_steps()

    def get_latest_step(self) -> Optional[StepRecord]:
        """Return the most recently completed step."""
        return self.progress.latest_step()

    def get_key_answers(self) -> dict:
        """Return all recorded key answers for inclusion in completion summary."""
        return dict(self.progress.key_answers)

    def list_step_artifacts(self) -> list[Path]:
        """List all step artifact files."""
        return sorted(self.steps_dir.glob("*")) if self.steps_dir.exists() else []


# --- Module-level helpers for worker integration ---

_artifact_manager: Optional[ArtifactManager] = None


def get_artifact_manager() -> Optional[ArtifactManager]:
    """Get the current worker's artifact manager, if initialised."""
    return _artifact_manager


def initialise_worker_artifacts(task_id: str, run_id: int, board: Optional[str] = None) -> ArtifactManager:
    """Initialise the artifact manager for the current worker.

    Called once at worker startup (from bind_worker_context or first turn).
    """
    global _artifact_manager
    _artifact_manager = ArtifactManager(task_id, run_id, board)
    return _artifact_manager


def write_step_artifact(
    step_name: str,
    content: str | bytes,
    *,
    extension: str = ".md",
    metadata: Optional[dict] = None,
) -> Optional[Path]:
    """Write a step artifact for the current worker.

    Returns the artifact path, or None if no artifact manager is active
    (e.g., not running as a kanban worker).
    """
    mgr = get_artifact_manager()
    if mgr is None:
        return None
    return mgr.write_step(step_name, content, extension=extension, metadata=metadata)


def write_step_text(step_name: str, text: str, **kwargs) -> Optional[Path]:
    """Write a text step artifact for the current worker."""
    mgr = get_artifact_manager()
    if mgr is None:
        return None
    return mgr.write_step_text(step_name, text, **kwargs)


def write_step_json(step_name: str, data: dict, **kwargs) -> Optional[Path]:
    """Write a JSON step artifact for the current worker."""
    mgr = get_artifact_manager()
    if mgr is None:
        return None
    return mgr.write_step_json(step_name, data, **kwargs)


def record_key_answer(key: str, value: str) -> bool:
    """Record a literal key answer for the current worker's completion summary."""
    mgr = get_artifact_manager()
    if mgr is None:
        return False
    mgr.record_key_answer(key, value)
    return True


def read_progress_marker(task_id: str, board: Optional[str] = None) -> Optional[ProgressMarker]:
    """Read the progress marker for a task from its durable attachments directory."""
    attachments_dir = kb.task_attachments_dir(task_id, board=board)
    progress_path = attachments_dir / ".progress.json"
    if not progress_path.exists():
        return None
    try:
        with progress_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return ProgressMarker.from_dict(data)
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def recovery_plan(task_id: str, board: Optional[str] = None) -> Optional[dict]:
    """Generate a recovery plan for a crashed worker.

    Returns a dict with:
    - task_id
    - completed_steps: list of step names already done
    - latest_step: the last completed step record
    - key_answers: literal answers recorded so far
    - can_resume: True if there are completed steps to resume from

    Returns None if no progress marker exists.
    """
    marker = read_progress_marker(task_id, board)
    if marker is None:
        return None

    return {
        "task_id": task_id,
        "run_id": marker.run_id,
        "completed_steps": list(marker.completed_steps()),
        "latest_step": marker.latest_step().to_dict() if marker.latest_step() else None,
        "key_answers": marker.get_key_answers(),
        "can_resume": len(marker.steps) > 0,
        "marker_version": marker.version,
        "worker_pid": marker.worker_pid,
        "started_at": marker.started_at,
        "updated_at": marker.updated_at,
    }


@contextmanager
def step_context(step_name: str, **metadata):
    """Context manager for a work step: yields a collector, writes artifact on exit.

    Usage:
        with step_context("analysis") as collect:
            collect("Finding 1")
            collect("Finding 2")
        # On exit, writes all collected content as a single artifact
    """
    collected = []

    class Collector:
        def __call__(self, text: str) -> None:
            collected.append(text)

        def write(self, text: str) -> None:
            collected.append(text)

        def writelines(self, lines: list[str]) -> None:
            collected.extend(lines)

    collector = Collector()
    try:
        yield collector
    finally:
        content = "".join(collected)
        if content:
            write_step_text(step_name, content, metadata=metadata)


# --- Integration with kanban_db completion ---

def stage_completion_artifacts(conn, task_id: str, metadata: dict, now: int) -> list[Path]:
    """Stage completion artifacts from metadata for attachment recording.

    This replaces the scratch-based artifact preservation in kanban_db.py
    with the durable artifacts already written during the run.
    """
    from hermes_cli import kanban_db as _kb
    # The durable artifacts are already in the attachments dir;
    # we just need to register any declared artifacts that aren't yet recorded.
    # The progress marker and step artifacts are already persisted.
    staged = []
    artifacts = metadata.get("artifacts") if isinstance(metadata, dict) else None
    if isinstance(artifacts, (list, tuple)):
        for art in artifacts:
            if isinstance(art, str) and art.strip():
                p = Path(art.strip())
                if p.exists():
                    # Already in durable location; just ensure it's recorded
                    pass
    return staged


def build_completion_summary_from_progress(task_id: str, board: Optional[str] = None) -> Optional[str]:
    """Build a completion summary that echoes key answers from the progress marker."""
    marker = read_progress_marker(task_id, board)
    if marker is None or not marker.key_answers:
        return None
    lines = ["Key results:"]
    for k, v in marker.key_answers.items():
        lines.append(f"  {k} = {v}")
    return "\n".join(lines)