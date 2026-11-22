"""W1 wake-emitter core: durable event producer on ``on_session_end``.

Component only. Synchronous, no background execution of any kind. The feature
flag defaults to OFF; when off the hook returns ``DISABLED`` and performs no I/O.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Bounded ACK reconciliation: at most three events per invocation, one pane_read
# subprocess each, hard 60 s total budget shared across the invocation.
ACK_MAX_EVENTS = 3
ACK_BUDGET_SECONDS = 60.0
ACK_HORIZON_SECONDS = 1800.0
# Consecutive observer failures tolerated per event before it is given up on.
ACK_MAX_ERRORS = 3
# Truthful ACK-side refusal: the bound conversation rotated after the send, so
# its pane is no longer the conversation that received this event.
ACK_GENERATION_ROLLED = "ACK_GENERATION_ROLLED"
# W1d fail-closed ACK outcomes. All are retryable: later evidence may settle.
ACK_GENERATION_UNKNOWN = "ACK_GENERATION_UNKNOWN"
ACK_DELIVERY_UNPROVEN = "ACK_DELIVERY_UNPROVEN"
ACK_TIME_UNPROVEN = "ACK_TIME_UNPROVEN"
ACK_BEFORE_DELIVERY = "ACK_BEFORE_DELIVERY"
# W1e: a readback with no turn timestamps can still be bound by turn order, but
# must be labelled so it is never read as a wall-clock-verified ACK.
ACKED_ORDER_ONLY = "ACKED_ORDER_ONLY"
# Clock skew tolerated when binding ACK turn time to delivery time. Named config,
# not a literal buried in the sweep: operators set it via ack_skew_seconds.
ACK_SKEW_SECONDS = 30.0
# Exactly one wider pane window is allowed when a reply's delivery turn is
# truncated out of the readback; it shares the existing ACK sweep deadline.
ACK_REREAD_WINDOW = 400
# Where an event was delivered, frozen on its write-ahead attempt row at send
# time and carried onto the ACK outcome so a fallback event is labelled.
ACK_SOURCE_BOUND = "bound"
ACK_SOURCE_GLOBAL_FALLBACK = "global_fallback"
ACK_SOURCE_LEGACY = "legacy"
# Bounded hook: the host abandons an on_session_end callback at 30 s, so the
# whole hook must finish inside this budget (default 20 s, well under 30).
HOOK_BUDGET_SECONDS = 20.0
# Margin reserved inside the budget for the journal record and the status write.
HOOK_RESERVE_SECONDS = 0.5
# Non-terminal: the send was still running when the hook budget expired.
DELIVERY_PENDING = "DELIVERY_PENDING"
# Retry staleness: a still-retryable event older than this is settled, not sent.
RETRY_HORIZON_SECONDS = 900.0
ABANDONED_STALE = "ABANDONED_STALE"
# turn_id absent: no positional identity, no dedupe; the event uuid is the identity.
AT_LEAST_ONCE_KEY = "none:at-least-once"
STATUS_SUFFIX = ".status.json"
# Durable-binding outcomes: truthful non-delivery, never a silent global fallback.
UNBOUND_TASK = "UNBOUND_TASK"
STALE_GENERATION = "STALE_GENERATION"
BINDING_LANE_UNRESOLVED = "BINDING_LANE_UNRESOLVED"
BINDING_INVALID = "BINDING_INVALID"
# Default-deny scope gate: one stable status for every out-of-scope turn.
SKIPPED_OUT_OF_SCOPE = "SKIPPED_OUT_OF_SCOPE"
# Cross-process writer coordination. An advisory file lock held only for one
# critical section (a journal append, or the ATTEMPTED/send decision). It is a
# lock, not a background service or worker: nothing is started or run later.
LOCK_TIMEOUT_SECONDS = 150.0
_LOCK_DEPTH = {}
_LOCK_HANDLE = {}


class PaneReadNoOutput(Exception):
    """pane_read exited 0 but carried no parseable turn document."""


class UnresolvedLane(Exception):
    """No target lane is configured, so pane_read cannot be aimed."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc(raw):
    """Parse an ISO-8601 stamp to an aware datetime, or None when absent/invalid."""
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _turn_utc(turn):
    """Timestamp carried by a pane turn record, or None when the readback has none."""
    if not isinstance(turn, dict):
        return None
    for key in ("utc", "timestamp"):
        raw = turn.get(key)
        if isinstance(raw, str) and raw:
            return raw
    return None


# -- cross-process advisory lock ------------------------------------------
# The lock file lives next to the journal. It is opened (not created-new), one
# byte at offset 0 is locked, and the descriptor is closed on release. Locks are
# advisory and held only for the duration of one critical section, so a crashed
# holder releases them with its process.
def _open_lock_handle(path):
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except OSError:
        return None


def _platform_lock(descriptor, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


def _platform_unlock(descriptor):
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        pass


def _acquire_journal_lock(path, timeout_seconds):
    """Reentrant-per-process advisory lock; True when this process holds it."""
    depth = _LOCK_DEPTH.get(path, 0)
    if depth:
        _LOCK_DEPTH[path] = depth + 1
        return True
    descriptor = _open_lock_handle(path)
    if descriptor is None:
        return False
    if not _platform_lock(descriptor, timeout_seconds):
        os.close(descriptor)
        return False
    _LOCK_DEPTH[path] = 1
    _LOCK_HANDLE[path] = descriptor
    return True


def _release_journal_lock(path):
    depth = _LOCK_DEPTH.get(path, 0)
    if depth > 1:
        _LOCK_DEPTH[path] = depth - 1
        return
    descriptor = _LOCK_HANDLE.pop(path, None)
    _LOCK_DEPTH.pop(path, None)
    if descriptor is not None:
        _platform_unlock(descriptor)
        try:
            os.close(descriptor)
        except OSError:
            pass


# -- sender output contract -----------------------------------------------
# The real sender prints line-oriented markers. A line beginning ``REFUSED:``
# means NOTHING was typed or submitted; ``GUARD_COMPLETE key=...`` (with its
# key) and the sender's own ``DELIVERED`` success lines are the only delivery
# proofs. Anything else is not a delivery.
def _classify_sender_output(output, returncode):
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    for line in lines:
        if line.startswith("REFUSED:"):
            return "REFUSED:" + line[len("REFUSED:"):].strip()
    if returncode == 0:
        for line in lines:
            if line.startswith("GUARD_COMPLETE") and "key=" in line:
                return "DELIVERED_UNACKED"
        for line in lines:
            if line.startswith("DELIVERED to ") or line.startswith("DELIVERED_BY_RECEIPT to "):
                return "DELIVERED_UNACKED"
        # Legacy BUSY no-send that is not a REFUSED line. The real sender's BUSY
        # refusal begins REFUSED: and is caught above. Non-terminal: never a
        # delivery, so a later invocation may still deliver.
        for line in lines:
            if "BUSY" in line:
                return "DELIVERED_BUSY"
        return "REFUSED:NO_DELIVERY_PROOF"
    for line in lines:
        if line.startswith("UNCERTAIN"):
            # An UNCERTAIN line is ambiguous: not a delivery proof and not a
            # terminal state. Normalise it to DELIVERY_PENDING so it settles
            # only through the one bounded DELIVERY_PENDING sweep, on pane
            # evidence alone. Never dedupe it as a completed delivery.
            return DELIVERY_PENDING
    return "REFUSED:EXIT_%s" % returncode


def _is_no_send_outcome(outcome):
    """Definitive no-send: the sender refused before submitting anything.

    A BUSY pane is the named case; the real sender's BUSY refusal reaches us as
    REFUSED: with "busy" in its text. Keeping the write-ahead intent for these
    would strand a retry as RECONCILE_UNPROVEN, so they void the intent instead.
    """
    if not isinstance(outcome, str):
        return False
    return outcome == "DELIVERED_BUSY" or (
        outcome.startswith("REFUSED:") and "busy" in outcome.lower())


def resolve_origin(peer_map: dict, lane: str | None):
    """Fail-closed origin lookup. Never guess a conversation or generation.

    Returns ``{"conv": ..., "generation": ...}`` only when the lane exists,
    is not retired, and carries BOTH a conversation and a generation.
    """
    if not lane:
        return None
    entry = (peer_map.get("lanes") or {}).get(lane)
    if not isinstance(entry, dict):
        return None
    if entry.get("retired_unbound") or entry.get("retired") is True or entry.get("state") == "RETIRED":
        return None
    conv = entry.get("conv")
    generation = entry.get("generation")
    if not conv or generation is None:
        return None
    return {"conv": conv, "generation": generation}


def _pane_document(stdout):
    """Return pane_read's final JSON document, or None when it printed none."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("turns"), list):
            return data
    return None


def _pane_turns(stdout):
    """Extract turn records from pane_read's final JSON document.

    The live panes match the ``main-div-group`` selector, so every role comes back
    ``unknown`` and role cannot be the acceptance mechanism. Each text is instead
    filtered by ``_ack_candidate``: a turn counts only when it reads as a reply,
    never as our own outbound envelope.
    """
    data = _pane_document(stdout)
    if data is None:
        raise PaneReadNoOutput()
    # Keep the whole record: its ``utc`` field is needed for timestamp binding.
    return [turn for turn in data["turns"]
            if isinstance(turn, dict) and isinstance(turn.get("text"), str)]


# Markers of our own outbound envelope. The emitter's sender submits the bare nonce
# "EVENT-ID: <uuid>" (see _sender_argv), so that marker is required too: without it the
# delivered user turn would satisfy its own ACK.
ENVELOPE_MARKERS = ("HERMES-CTL 1", "MSG-ID:", "FROM: hermes:", "EVENT-ID:")
# A main-div-group block can embed our message and the reply in one turn.
REPLY_SPLIT = "ChatGPT said:"
# Full outbound envelopes from other Hermes senders. The wake body itself is the
# bare nonce "EVENT-ID: <uuid>", so that marker is NOT excluded here: it IS the
# delivery proof. These markers identify a different sender's envelope, which
# must never be read as our wake body.
DELIVERY_ENVELOPE_MARKERS = ("HERMES-CTL 1", "MSG-ID:", "FROM: hermes:")
# Write-ahead intent. kind="attempt" keeps it out of the terminal outcome log
# (and its consumers) while still recording "a send may have happened".
ATTEMPTED = "ATTEMPTED"
# Bounded read for resume reconciliation; unknown state must fail closed.
RECONCILE_TIMEOUT_SECONDS = 60.0
# Hard cap on one sender subprocess when no hook deadline is active.
SENDER_TIMEOUT_SECONDS = 120.0


def _ack_candidate(text, event_id):
    """True when ``text`` is a reply carrying ``event_id``, never our own envelope.

    A block that embeds both our message and the reply is split at the last
    ``ChatGPT said:`` and only the reply half is considered; text that still carries
    an envelope marker after the split is rejected.
    """
    if not isinstance(text, str) or not text:
        return False
    candidate = text.rsplit(REPLY_SPLIT, 1)[-1] if REPLY_SPLIT in text else text
    if any(marker in candidate for marker in ENVELOPE_MARKERS):
        return False
    return event_id in candidate


def _delivery_candidate(text, event_id):
    """True when ``text`` is a delivered outbound turn carrying ``event_id``.

    Same envelope discipline as the ACK matcher, applied to the outbound half:
    a combined block is split at the last ``ChatGPT said:`` and only the part
    before it is considered, because the reply half is the assistant echoing
    our id. A candidate carrying another sender's full outbound envelope is
    rejected. The bare nonce marker ``EVENT-ID:`` is the wake body itself, so
    it is not excluded here (unlike in the ACK matcher).
    """
    if not isinstance(text, str) or not text:
        return False
    candidate = text.rsplit(REPLY_SPLIT, 1)[0] if REPLY_SPLIT in text else text
    if any(marker in candidate for marker in DELIVERY_ENVELOPE_MARKERS):
        return False
    return event_id in candidate


def _delivery_present(turns, event_id):
    return any(_delivery_candidate(_turn_text(turn), event_id) for turn in turns)


def _turn_text(turn):
    """Turn text from a stub string or a pane record, never a non-string."""
    if isinstance(turn, str):
        return turn
    if isinstance(turn, dict) and isinstance(turn.get("text"), str):
        return turn["text"]
    return ""


def _delivery_turn_index(turns, event_id):
    """Index of the outbound wake for ``event_id`` in a pane, or None.

    ``EVENT-ID: <id>`` is the bare nonce the sender submits, so it marks the
    delivery turn itself; the reply line ``WAKE-ACK <id>`` does not carry it.
    """
    marker = "EVENT-ID: %s" % event_id
    found = None
    for index, turn in enumerate(turns):
        if marker in _turn_text(turn):
            found = index
    return found


def _ack_turn_index(turns, event_id):
    """Index of an assistant turn that acknowledges ``event_id`` AFTER delivery.

    The nonce must be in the reply half of the turn (``_ack_candidate``), and
    the turn must follow the outbound delivery turn in the pane. If a combined
    turn embeds both halves, the reply follows the delivery inside that turn.
    A pane window that omits the delivery turn proves nothing and is refused.
    """
    delivery_index = _delivery_turn_index(turns, event_id)
    if delivery_index is None:
        return None
    for index, turn in enumerate(turns):
        text = _turn_text(turn)
        if not _ack_candidate(text, event_id):
            continue
        if delivery_index is not None:
            if index < delivery_index:
                continue
            if index == delivery_index and REPLY_SPLIT not in text:
                continue
        return index
    return None


# -- durable task binding store -------------------------------------------
# In-process mutual exclusion complements the cross-process advisory lock: the
# journal lock is reentrant per process, so two threads need their own gate.
_BINDING_THREAD_LOCKS = {}
_BINDING_THREAD_GUARD = threading.Lock()


def _binding_thread_lock(path):
    with _BINDING_THREAD_GUARD:
        lock = _BINDING_THREAD_LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _BINDING_THREAD_LOCKS[path] = lock
        return lock


class TaskBindingStore:
    """Durable JSON-lines task/session -> conversation+generation bindings.

    One append-only row per bind; later rows for the same key supersede earlier
    ones. A plain file reader needs no gateway, daemon or dependency. Reads and
    writes are strict-equality keyed: task A can never resolve to task B's row.
    """

    def __init__(self, path, lock_timeout_seconds=LOCK_TIMEOUT_SECONDS):
        self.path = Path(path)
        self.lock_path = Path(str(self.path) + ".lock")
        self.lock_timeout_seconds = float(lock_timeout_seconds)

    def _rows(self):
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("kind") == "binding":
                rows.append(row)
        return rows

    def _append(self, row):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
        descriptor = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
        try:
            view = memoryview(line)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short binding write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def bind(self, *, task_id=None, session_id=None, conversation_id=None, generation=None,
             bound_by=None, bound_utc=None):
        """Append one binding atomically. Never write a partial or torn row."""
        if not task_id and not session_id:
            raise ValueError("a binding needs task_id or session_id")
        if not conversation_id or generation is None:
            raise ValueError("a binding needs conversation_id and generation")
        row = {"kind": "binding", "task_id": task_id, "session_id": session_id,
               "conversation_id": conversation_id, "generation": generation,
               "bound_utc": bound_utc or _utc_now(), "bound_by": bound_by}
        with _binding_thread_lock(str(self.path)):
            if not _acquire_journal_lock(str(self.lock_path), self.lock_timeout_seconds):
                raise OSError("binding lock unavailable")
            try:
                self._append(row)
            finally:
                _release_journal_lock(str(self.lock_path))
        return dict(row)

    def lookup(self, task_id=None, session_id=None):
        """Latest exact-key binding only; task_id takes precedence over session."""
        by_task = {}
        by_session = {}
        for row in self._rows():
            if row.get("task_id"):
                by_task[row["task_id"]] = row
            if row.get("session_id"):
                by_session[row["session_id"]] = row
        if task_id is not None and task_id in by_task:
            return dict(by_task[task_id])
        if session_id is not None and session_id in by_session:
            return dict(by_session[session_id])
        return None


class WakeEmitter:
    """Append-only journal + guarded sender invocation. One instance per process."""

    def __init__(self, *, journal_path, peer_map_path, sender_path, pane_read_path=None,
                 ack_horizon_seconds=ACK_HORIZON_SECONDS, ack_skew_seconds=ACK_SKEW_SECONDS,
                 lane=None, enabled=False, logger=None,
                 lock_timeout_seconds=None, scope=None, python_executable=None,
                 hook_budget_seconds=HOOK_BUDGET_SECONDS,
                 retry_horizon_seconds=RETRY_HORIZON_SECONDS,
                 binding_path=None, allow_unbound_global_fallback=False, bindings=None):
        self.journal_path = Path(journal_path)
        self.peer_map_path = Path(peer_map_path)
        # One interpreter governs every child this emitter spawns. Cron workers
        # run a tools Python without third-party modules, so operators can point
        # this at an interpreter that has them; unset keeps the historical value.
        self.python_executable = str(python_executable) if python_executable else sys.executable
        self.sender_path = str(sender_path)
        self.pane_read_path = str(pane_read_path) if pane_read_path else str(
            Path.home() / ".hermes" / "scripts" / "pane_read.py"
        )
        self.ack_horizon_seconds = float(ack_horizon_seconds)
        self.ack_skew_seconds = float(ack_skew_seconds)
        self.lane = lane
        self.enabled = bool(enabled)
        self.scope = scope
        self.logger = logger
        # Liveness record lives next to the journal so an operator can see the
        # path is alive without reading the journal.
        self.status_path = self.journal_path.with_name(self.journal_path.stem + STATUS_SUFFIX)
        # Advisory writer lock lives next to the journal; one byte at offset 0.
        self.lock_path = Path(str(self.journal_path) + ".lock")
        self.lock_timeout_seconds = float(
            LOCK_TIMEOUT_SECONDS if lock_timeout_seconds is None else lock_timeout_seconds)
        # Whole-hook wall-clock budget; the host abandons the callback at 30 s.
        self.hook_budget_seconds = float(hook_budget_seconds)
        self.retry_horizon_seconds = float(retry_horizon_seconds)
        # A binding store activates fail-closed resolution. No store path keeps
        # legacy component-instantiation behaviour; operators opt back in with
        # allow_unbound_global_fallback. build_from_ctx always configures a store.
        self.bindings = bindings if bindings is not None else (
            TaskBindingStore(binding_path) if binding_path else None)
        self.allow_unbound_global_fallback = bool(allow_unbound_global_fallback)

    # -- journal -----------------------------------------------------------
    def _rows(self):
        if not self.journal_path.exists():
            return []
        rows = []
        for line in self.journal_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def _write_row(self, row):
        """One line, one append syscall. The caller owns the lock when it matters."""
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
        descriptor = os.open(str(self.journal_path),
                             os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
        try:
            view = memoryview(line)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short journal write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @contextlib.contextmanager
    def _locked(self):
        """Bounded cross-process lock; yields True when held, False on timeout."""
        if not _acquire_journal_lock(str(self.lock_path), self.lock_timeout_seconds):
            yield False
            return
        try:
            yield True
        finally:
            _release_journal_lock(str(self.lock_path))

    def _append(self, row):
        """Append one row under the lock. Never drop a line: if the bounded wait
        expires, write best-effort rather than lose the row. The send decision
        path fails closed on the same timeout instead of writing."""
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        if not _acquire_journal_lock(str(self.lock_path), self.lock_timeout_seconds):
            self._write_row(row)
            return False
        try:
            self._write_row(row)
            return True
        finally:
            _release_journal_lock(str(self.lock_path))

    @staticmethod
    def _event_row(event_id, dedupe_key, payload):
        return {
            "kind": "event",
            "event_id": event_id,
            "dedupe_key": dedupe_key,
            "utc": _utc_now(),
            "session_id": payload.get("session_id"),
            "task_id": payload.get("task_id"),
            "turn_id": payload.get("turn_id"),
            "turn_exit_reason": payload.get("turn_exit_reason"),
            "model": payload.get("model"),
            "platform": payload.get("platform"),
        }

    def _record_lock_timeout(self, event_id):
        """Fail-closed record: the lock is unavailable, so no send may happen."""
        self._write_row({"kind": "outcome", "event_id": event_id,
                         "utc": _utc_now(), "outcome": "LOCK_TIMEOUT"})

    def _log(self, message):
        log = self.logger
        if log is not None and hasattr(log, "info"):
            try:
                log.info(message)
            except BaseException:
                pass

    # -- public seam -------------------------------------------------------
    def _in_scope(self, payload):
        """Default-deny scope gate. A malformed scope rejects; never raises.

        Absent scope keeps the historical emit-on-every-completed-turn contract.
        Present scope is an allowlist: every configured rule must pass, and a
        scope with no effective rule allows nothing. ``task_id_prefix_allow`` is
        an additional gate: when present and non-empty, only those task_id
        prefixes are in scope, so minted/uuid, interactive, gateway and induced
        turns are skipped instead of waking the lane.
        """
        scope = self.scope
        if scope is None:
            return True
        try:
            if not isinstance(scope, dict):
                return False
            session_ids = scope.get("session_ids", [])
            need_task = scope.get("require_task_id", False)
            if not isinstance(session_ids, list) or not isinstance(need_task, bool):
                return False
            # Additional deny only: a malformed exclude list rejects the turn,
            # an empty or absent one is a no-op that cannot widen the allowlist.
            excluded = scope.get("task_id_exclude_prefixes", [])
            if not isinstance(excluded, list) or any(not isinstance(p, str) for p in excluded):
                return False
            # Additional allow gate: a malformed value rejects, an empty or
            # absent one is a no-op so existing scopes keep today's behaviour.
            allowed = scope.get("task_id_prefix_allow", [])
            if not isinstance(allowed, list) or any(not isinstance(p, str) for p in allowed):
                return False
            if not session_ids and not need_task and not allowed:
                return False
            if session_ids and payload.get("session_id") not in session_ids:
                return False
            task_id = payload.get("task_id")
            if need_task and not task_id:
                return False
            if isinstance(task_id, str) and any(task_id.startswith(p) for p in excluded):
                return False
            if allowed and (not isinstance(task_id, str)
                            or not any(task_id.startswith(p) for p in allowed)):
                return False
            return True
        except BaseException:
            return False

    def on_session_end(self, **payload):
        """Hook callback. Returns a terminal status string; never raises."""
        if not self.enabled:
            self._log("wake-emitter disabled; no event emitted")
            return "DISABLED"
        # One wall-clock budget per invocation, shared by the ack sweep, the
        # pane read and the send. The host abandons the callback at 30 s.
        self._hook_deadline = time.monotonic() + self.hook_budget_seconds
        try:
            outcome = self._on_session_end_enabled(payload)
        except BaseException as exc:
            # The enabled path is component code over files, locks and a
            # subprocess; any escaped fault becomes a status string, never a
            # raise into the turn_finalizer hook call.
            outcome = "INTERNAL_ERROR:%s" % type(exc).__name__
            self._record_internal_error(payload, exc)
        finally:
            self._hook_deadline = None
        # Liveness signal, written last so it can never mask or replace the
        # primary outcome. A BaseException here is swallowed too.
        try:
            self._write_status()
        except BaseException:
            pass
        return outcome

    def _record_internal_error(self, payload, exc):
        """Best-effort durable record of an escaped fault. Never raises."""
        outcome = "INTERNAL_ERROR:%s" % type(exc).__name__
        try:
            event_id = None
            turn_id = payload.get("turn_id")
            if turn_id:
                for row in self._rows():
                    if row.get("kind") == "event" and row.get("turn_id") == turn_id:
                        event_id = row.get("event_id")
                        break
            self._record(event_id or str(uuid.uuid4()), outcome)
        except BaseException as record_exc:
            # The journal itself is unwritable: fall back to the logger and
            # still return the status string. Format only type names: the
            # exception objects' own __str__ may itself raise.
            try:
                self._log("wake-emitter internal error %s (status not recorded: %s)"
                          % (type(exc).__name__, type(record_exc).__name__))
            except BaseException:
                pass

    def _deadline(self, cap_seconds):
        """The active hook deadline, never more than ``cap_seconds`` away.

        Outside a hook invocation there is no budget, so the cap alone applies.
        """
        deadline = getattr(self, "_hook_deadline", None)
        cap = time.monotonic() + cap_seconds
        return cap if deadline is None else min(deadline, cap)

    def _on_session_end_enabled(self, payload):
        # Scope runs first: a rejected turn must produce no event, no outcome,
        # no ack reconciliation and no send - it must look like it never happened.
        if not self._in_scope(payload):
            return SKIPPED_OUT_OF_SCOPE
        # Reconcile earlier deliveries first: this invocation was happening anyway,
        # so an observed ACK costs no new process and no new transport.
        self._reconcile_acks(payload)

        completed = bool(payload.get("completed"))
        failed = bool(payload.get("failed"))
        interrupted = bool(payload.get("interrupted"))
        if not (completed and not failed and not interrupted):
            return "SKIPPED"

        turn_id = payload.get("turn_id")
        # The dedupe decision and the event row are one critical section: two
        # concurrent invocations of the same turn must not mint two events.
        with self._locked() as held:
            if not held:
                return self._lock_timeout(payload)
            rows = self._rows()
            event_id = None
            resumed = False
            if turn_id:
                dedupe_key = f"turn:{turn_id}"
                existing = [row for row in rows
                            if row.get("kind") == "event" and row.get("dedupe_key") == dedupe_key]
                if existing:
                    event_id = existing[-1]["event_id"]
                    if self._has_delivery_outcome(rows, event_id):
                        return "DUPLICATE"
                    # The event row exists but no terminal delivery outcome was
                    # recorded. A write-ahead attempt row means a send may have
                    # happened, so reconcile against the pane; never blind-resend.
                    resumed = True
            if event_id is None:
                # No turn_id reaches this hook. A positional journal count is
                # not an identity (append shifts it, concurrent runs collide,
                # truncation and deletion alias distinct turns). Do not dedupe:
                # identify by the event's own uuid and accept at-least-once.
                event_id = str(uuid.uuid4())
                dedupe_key = AT_LEAST_ONCE_KEY if not turn_id else f"turn:{turn_id}"
                self._append(self._event_row(event_id, dedupe_key, payload))
        if resumed:
            return self._resume_event(event_id, payload)
        return self._deliver_and_record(event_id, payload)

    def _lock_timeout(self, payload):
        """Fail closed: the writer lock is unavailable, so no send may happen."""
        turn_id = payload.get("turn_id")
        event_id = None
        if turn_id:
            for row in self._rows():
                if row.get("kind") == "event" and row.get("turn_id") == turn_id:
                    event_id = row.get("event_id")
        if event_id is None:
            event_id = str(uuid.uuid4())
            dedupe_key = AT_LEAST_ONCE_KEY if not turn_id else f"turn:{turn_id}"
            self._write_row(self._event_row(event_id, dedupe_key, payload))
        self._record_lock_timeout(event_id)
        return "LOCK_TIMEOUT"

    @staticmethod
    def _has_delivery_outcome(rows, event_id):
        """True when a terminal delivery outcome exists for this event.

        Write-ahead intents (kind="attempt") are not outcomes. Non-terminal
        outcomes do not satisfy the duplicate check:
        ACK_CHECK_ERROR (observer failure after delivery), REFUSED: (the sender
        typed nothing), LOCK_TIMEOUT (this process failed closed), the legacy
        DELIVERED_BUSY no-send, INTERNAL_ERROR: (a transient internal fault),
        RECONCILE_UNPROVEN (the readback could not confirm the body),
        RECONCILE_UNAVAILABLE (the readback itself failed), DELIVERY_ERROR: (the
        sender process faulted) and DELIVERY_PENDING (the send outlived the hook
        budget) all leave the event deliverable. Each stays open for a later
        reconciliation; the attempt guard in _resume_event still makes a send
        impossible once a write-ahead intent exists.
        """
        for row in rows:
            if row.get("kind") == "outcome" and row.get("event_id") == event_id:
                if WakeEmitter._is_terminal_outcome(row.get("outcome")):
                    return True
        return False

    @staticmethod
    def _is_terminal_outcome(outcome):
        """False for the non-terminal faults that deliberately stay retryable."""
        if not isinstance(outcome, str):
            return False
        if outcome.startswith(("ACK_CHECK_ERROR:", "REFUSED:", "INTERNAL_ERROR:",
                               "RECONCILE_UNPROVEN", "DELIVERY_ERROR:",
                               "DELIVERY_PENDING")):
            return False
        return outcome not in ("DELIVERED_BUSY", "LOCK_TIMEOUT", "RECONCILE_UNAVAILABLE")

    def _settle_stale(self, event_id, rows):
        """Settle an aged, still-retryable event once; never send for it.

        Only the retry path calls this, so a fresh event and an already-terminal
        one are untouched. ABANDONED_STALE is terminal to _has_delivery_outcome,
        so a later invocation dedupes instead of recording a second settlement.
        """
        event_row = None
        for row in rows:
            if row.get("kind") == "event" and row.get("event_id") == event_id:
                event_row = row
                break
        if event_row is None:
            return False
        latest = None
        for row in rows:
            if row.get("kind") == "outcome" and row.get("event_id") == event_id:
                latest = row.get("outcome")
        if self._is_terminal_outcome(latest):
            return False
        age = self._age_seconds(event_row)
        if age is None or age <= self.retry_horizon_seconds:
            return False
        self._record(event_id, ABANDONED_STALE)
        return True

    def _deliver_and_record(self, event_id, payload):
        """Resolve the origin, then decide + ATTEMPTED + send + record.

        The lock spans the decision and the write-ahead intent: the intent row
        is appended and fsynced BEFORE the sender subprocess is invoked, so a
        death after the physical send is distinguishable from a death before
        it, and two processes cannot both decide to send the same event.

        The send target is resolved first from the durable task binding. An
        unbound task never silently uses the global lane (fail closed); a stale
        generation is refused before any send.
        """
        target = self._resolve_send_target(payload)
        if target["refusal"]:
            self._record(event_id, target["refusal"], target.get("details"))
            return target["refusal"]
        lane = target["lane"]
        origin = target["origin"]
        if origin is None:
            self._record(event_id, "BLOCKED_UNRESOLVED_ORIGIN")
            return "BLOCKED_UNRESOLVED_ORIGIN"
        argv = self._sender_argv(origin, event_id, lane)
        with self._locked() as held:
            if not held:
                self._record_lock_timeout(event_id)
                return "LOCK_TIMEOUT"
            rows = self._rows()
            if self._has_delivery_outcome(rows, event_id):
                return "DUPLICATE"
            if self._has_attempt(rows, event_id):
                # The single send gate: a write-ahead intent exists, so a send
                # may already have happened. Never blind-send.
                return "RECONCILE_UNAVAILABLE"
            if self._deadline(HOOK_BUDGET_SECONDS) - time.monotonic() <= HOOK_RESERVE_SECONDS:
                # No room left to send: leave the event open rather than write
                # a write-ahead intent for a send that never started.
                self._record(event_id, DELIVERY_PENDING)
                return DELIVERY_PENDING
            self._record_attempt(event_id, ATTEMPTED, argv, target)
            return self._send_and_record(event_id, argv)

    def _resolve_send_target(self, payload):
        """Return {origin, lane, refusal, details, lane_source}; fail closed when unbound."""
        task_id = payload.get("task_id")
        session_id = payload.get("session_id")
        binding = self.bindings.lookup(task_id, session_id) if self.bindings else None
        if binding is not None:
            target = self._resolve_from_binding(binding)
            target["lane_source"] = ACK_SOURCE_BOUND
            return target
        global_lane = self.lane or payload.get("lane")
        global_origin = self._resolve_origin(global_lane) if global_lane else None
        # The defect being closed: a resolvable global lane must NOT silently
        # carry an unbound task. With no store configured at all, legacy
        # component-instantiation behaviour is unchanged.
        if (self.bindings is not None and global_origin is not None
                and not self.allow_unbound_global_fallback):
            return {"origin": None, "lane": None, "refusal": UNBOUND_TASK,
                    "details": {"task_id": task_id, "session_id": session_id},
                    "lane_source": ACK_SOURCE_LEGACY}
        source = ACK_SOURCE_GLOBAL_FALLBACK if self.allow_unbound_global_fallback \
            else ACK_SOURCE_LEGACY
        return {"origin": global_origin, "lane": global_lane, "refusal": None,
                "lane_source": source}

    def _resolve_from_binding(self, binding):
        """Aim at the bound conversation; refuse any generation mismatch.

        The peer map is the authority for the conversation's current generation
        and its lane. A mismatch means the pane rotated: refuse before sending,
        recording both generations so the refusal is truthful and auditable.
        """
        conv = binding.get("conversation_id")
        bound_gen = binding.get("generation")
        if not conv or bound_gen is None:
            return {"origin": None, "lane": None, "refusal": BINDING_INVALID, "details": {}}
        try:
            peer = json.loads(self.peer_map_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            peer = {}
        lanes = peer.get("lanes") if isinstance(peer, dict) else None
        lane = None
        observed = None
        if isinstance(lanes, dict):
            for name, entry in lanes.items():
                if isinstance(entry, dict) and entry.get("conv") == conv:
                    lane, observed = name, entry.get("generation")
                    break
        details = {"conversation_id": conv, "bound_generation": bound_gen,
                   "observed_generation": observed}
        if lane is None:
            return {"origin": None, "lane": None, "refusal": BINDING_LANE_UNRESOLVED,
                    "details": details}
        if observed != bound_gen:
            return {"origin": None, "lane": None, "refusal": STALE_GENERATION,
                    "details": details}
        return {"origin": {"conv": conv, "generation": bound_gen},
                "lane": lane, "refusal": None, "details": details}

    def _resume_event(self, event_id, payload):
        """Reconcile an event that has no terminal delivery outcome.

        A write-ahead attempt row means a send may already have happened, so
        the pane is the only authority and the event is never resent: a body
        positively present becomes DELIVERED_UNCONFIRMED, an unreadable pane
        fails closed, and a readable tail without the body is RECONCILE_UNPROVEN
        (unknown, not absent). No attempt row means no send can have happened,
        so the event stays retry-eligible. An event older than retry_horizon_seconds
        is settled ABANDONED_STALE before any branch below: a late wake is worse
        than no wake, so it is never sent and never retried again.
        """
        rows = self._rows()
        if self._settle_stale(event_id, rows):
            return ABANDONED_STALE
        if not self._has_attempt(rows, event_id):
            # No write-ahead intent exists, so no subprocess was ever invoked
            # for this event and no send can have happened. Safe to deliver.
            return self._deliver_and_record(event_id, payload)

        lane = self.lane or payload.get("lane")
        turns = None
        if lane:
            try:
                turns = self._read_pane_turns(
                    lane, max(0.05, self._deadline(RECONCILE_TIMEOUT_SECONDS) - time.monotonic()))
            except Exception as exc:
                self._log("reconcile read failed for %s: %s: %s"
                          % (event_id, type(exc).__name__, exc))
        if turns is None:
            self._record(event_id, "RECONCILE_UNAVAILABLE")
            return "RECONCILE_UNAVAILABLE"
        if _delivery_present(turns, event_id):
            self._record(event_id, "DELIVERED_UNCONFIRMED")
            return "DELIVERED_UNCONFIRMED"
        # A readable tail that omits the body is not proof of absence. Stop:
        # no send, no second attempt; the event stays open for a human or a
        # later durable reconciliation.
        self._record(event_id, "RECONCILE_UNPROVEN",
                     {"lane": lane, "turns_read": len(turns)})
        return "RECONCILE_UNPROVEN"

    def _send_and_record(self, event_id, argv):
        """One bounded send. The hook budget caps the wait, never the event.

        The send seam stays one-argument (callers and tests wrap it); the
        bound is handed over as a deadline. A sender that cannot finish in
        budget is killed by ``subprocess.run`` and recorded DELIVERY_PENDING
        (non-terminal): the write-ahead attempt row already forbids a second
        physical send, and the next invocation reconciles the pane.
        """
        timeout = max(0.05, self._deadline(HOOK_BUDGET_SECONDS) - time.monotonic()
                      - HOOK_RESERVE_SECONDS)
        self._sender_deadline = time.monotonic() + timeout
        try:
            outcome = self._run_sender(argv)
        except subprocess.TimeoutExpired:
            outcome = DELIVERY_PENDING
        except Exception as exc:
            outcome = f"DELIVERY_ERROR:{type(exc).__name__}"
        finally:
            self._sender_deadline = None
        self._record(event_id, outcome)
        if _is_no_send_outcome(outcome):
            # A BUSY/refused pane had no physical send. Void the write-ahead
            # intent so the next idle invocation retries instead of reconciling
            # an intent that never represented a delivered message.
            self._void_attempt(event_id, outcome)
        return outcome

    def _resolve_origin(self, lane):
        try:
            peer_map = json.loads(self.peer_map_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            peer_map = {}
        return resolve_origin(peer_map, lane)

    @staticmethod
    def _has_attempt(rows, event_id):
        """True when a live write-ahead send intent exists for this event.

        A later attempt_void cancels only the attempts before it; a fresh send
        after the void is still a live intent. This is what lets a definitive
        BUSY no-send retry on the next idle invocation instead of reconciling.
        """
        last_void = -1
        for index, row in enumerate(rows):
            if row.get("kind") == "attempt_void" and row.get("event_id") == event_id:
                last_void = index
        for index, row in enumerate(rows):
            if (row.get("kind") == "attempt" and row.get("event_id") == event_id
                    and index > last_void):
                return True
        return False

    def _void_attempt(self, event_id, reason):
        """Cancel a write-ahead intent when the sender definitively did not send."""
        self._append({"kind": "attempt_void", "event_id": event_id,
                      "utc": _utc_now(), "reason": reason})

    def _record_attempt(self, event_id, stage, argv, target=None):
        """Write-ahead intent: a send may have happened. No secrets in argv.

        The binding-resolved lane/conversation/generation are frozen here so a
        later ACK sweep reads the lane the event was DELIVERED to, even if the
        binding or peer map changes before the reply arrives.
        """
        row = {"kind": "attempt", "event_id": event_id, "utc": _utc_now(),
               "outcome": stage, "argv": list(argv)}
        if target is not None:
            origin = target.get("origin") or {}
            row["delivery_lane"] = target.get("lane")
            row["delivery_conv"] = origin.get("conv")
            row["delivery_generation"] = origin.get("generation")
            row["lane_source"] = target.get("lane_source") or ACK_SOURCE_LEGACY
        self._append(row)

    def record_assistant_ack(self, event_id, message_ref=None, matched_utc=None,
                             lane=None, lane_source=None, conversation_id=None,
                             generation=None, outcome="DELIVERED_ACKED",
                             ack_binding="time_verified"):
        """Called once the correlated assistant readback is observed.

        ``outcome``/``ack_binding`` let a timestamp-less readback settle as
        ACKED_ORDER_ONLY without ever being confused with a time-verified ACK.
        """
        if not any(row.get("kind") == "event" and row.get("event_id") == event_id for row in self._rows()):
            return False
        row = {"kind": "outcome", "event_id": event_id, "utc": _utc_now(),
               "outcome": outcome, "ack_binding": ack_binding}
        if message_ref:
            row["ack_message_ref"] = message_ref
        if matched_utc:
            row["ack_utc"] = matched_utc
        # Label where this ACK was resolved from; a global-fallback event must
        # be distinguishable from a binding-bound one after the fact.
        if lane is not None:
            row["ack_lane"] = lane
        if lane_source is not None:
            row["ack_lane_source"] = lane_source
        if conversation_id is not None:
            row["ack_conversation_id"] = conversation_id
        if generation is not None:
            row["ack_generation"] = generation
        self._append(row)
        return True

    # -- ACK reconciliation -----------------------------------------------
    def _reconcile_acks(self, payload):
        """Bounded, best-effort ACK sweep. Never raises, never marks ACKED by inference."""
        try:
            self._reconcile_acks_inner(payload)
        except Exception as exc:
            self._log("ack reconciliation error: %s: %s" % (type(exc).__name__, exc))
        return None

    def _peer_generation(self, conv):
        """Current generation for a conversation in the peer map, or None."""
        if not conv:
            return None
        try:
            peer = json.loads(self.peer_map_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        lanes = peer.get("lanes") if isinstance(peer, dict) else None
        if not isinstance(lanes, dict):
            return None
        for entry in lanes.values():
            if isinstance(entry, dict) and entry.get("conv") == conv:
                return entry.get("generation")
        return None

    def _readback_supports_window(self):
        """True only when the configured readback advertises --window.

        Never pass an unknown argument: the live pane_read.py has no --window
        flag, so the wider re-read is skipped rather than sent as a bad argv.
        """
        try:
            text = Path(self.pane_read_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return "--window" in text

    def _ack_delivery(self, event_row, rows, global_lane):
        """Where an event was DELIVERED, frozen on its send-time attempt row.

        The binding resolution is recorded in the write-ahead intent before the
        sender runs, so reading it here cannot drift with a later rebind and
        cannot confuse two conversations that hold the same nonce. An attempt
        with no frozen resolution (written before this field existed) keeps the
        historical configured-lane behaviour.
        """
        event_id = event_row.get("event_id")
        last_void = -1
        for index, row in enumerate(rows):
            if row.get("kind") == "attempt_void" and row.get("event_id") == event_id:
                last_void = index
        for index in range(len(rows) - 1, -1, -1):
            row = rows[index]
            if (row.get("kind") == "attempt" and row.get("event_id") == event_id
                    and index > last_void and row.get("delivery_lane")):
                return {"lane": row.get("delivery_lane"),
                        "conv": row.get("delivery_conv"),
                        "generation": row.get("delivery_generation"),
                        "source": row.get("lane_source") or ACK_SOURCE_LEGACY,
                        "utc": row.get("utc")}
        return {"lane": global_lane, "conv": None, "generation": None,
                "source": ACK_SOURCE_LEGACY, "utc": event_row.get("utc")}

    def _reconcile_acks_inner(self, payload):
        global_lane = self.lane or payload.get("lane")
        rows = self._rows()
        latest = {}
        for row in rows:
            if row.get("kind") == "outcome" and row.get("event_id") is not None:
                latest[row["event_id"]] = row.get("outcome")

        candidates = []
        for row in reversed(rows):
            if row.get("kind") != "event":
                continue
            # Terminal states (ACKED / UNACKED_EXPIRED / REFUSED:*) are never re-marked.
            # ACK_CHECK_ERROR is an observer failure, not a state change: the event
            # stays deliverable-unacked and is retried on a later invocation.
            # DELIVERED_UNCONFIRMED is terminal for delivery but still eligible for
            # ACK reconciliation. DELIVERY_PENDING is open: the send outlived the
            # hook budget, so this sweep settles it from the pane, never by resend.
            state = latest.get(row.get("event_id"))
            retryable = (DELIVERY_PENDING, "DELIVERED_UNACKED", "DELIVERED_UNCONFIRMED",
                         ACK_GENERATION_UNKNOWN, ACK_DELIVERY_UNPROVEN,
                         ACK_TIME_UNPROVEN, ACK_BEFORE_DELIVERY)
            if state not in retryable and not (
                isinstance(state, str) and state.startswith("ACK_CHECK_ERROR:")
            ):
                continue
            candidates.append(row)
            if len(candidates) >= ACK_MAX_EVENTS:
                break
        if not candidates:
            return

        # One delivery resolution PER EVENT, from its own durable send-time
        # record - never the one configured lane for every event.
        deliveries = {row["event_id"]: self._ack_delivery(row, rows, global_lane)
                      for row in candidates}

        deadline = self._deadline(ACK_BUDGET_SECONDS)
        for row in candidates:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._log("ack reconciliation budget exhausted; remaining events wait")
                break
            event_id = row["event_id"]
            pending = latest.get(event_id) == DELIVERY_PENDING
            if self._consecutive_ack_errors(rows, event_id) >= ACK_MAX_ERRORS:
                # Retry budget exhausted: record the terminal give-up once and stop
                # retrying this event, so a broken observer cannot grow the journal.
                self._record(event_id, "ACK_CHECK_GAVE_UP")
                continue
            delivery = deliveries[event_id]
            lane = delivery["lane"]
            if not lane:
                if pending:
                    self._record(event_id, "RECONCILE_UNAVAILABLE")
                else:
                    self._note_ack_failure(rows, event_id, UnresolvedLane.__name__)
                continue
            # Generation safety on the ACK side: a conversation that rolled past
            # the recorded generation no longer holds the delivered reply, so it
            # cannot acknowledge this event. Record the truthful refusal.
            recorded_gen = delivery["generation"]
            if recorded_gen is not None:
                observed = self._peer_generation(delivery["conv"])
                if observed is None:
                    # Absence is not a match: never settle from it. Retryable so
                    # a later peer-map entry may still settle this event.
                    if latest.get(event_id) != ACK_GENERATION_UNKNOWN:
                        self._record(event_id, ACK_GENERATION_UNKNOWN,
                                     {"conversation_id": delivery["conv"],
                                      "recorded_generation": recorded_gen})
                    continue
                if observed != recorded_gen:
                    self._record(event_id, ACK_GENERATION_ROLLED,
                                 {"conversation_id": delivery["conv"],
                                  "recorded_generation": recorded_gen,
                                  "observed_generation": observed})
                    continue
            try:
                turns = self._read_pane_turns(lane, remaining)
                if (any(_ack_candidate(_turn_text(t), event_id) for t in turns)
                        and _delivery_turn_index(turns, event_id) is None
                        and self._readback_supports_window()):
                    # Hole 3: one bounded wider window inside the same sweep
                    # deadline. Never accept a reply found on its own.
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        turns = self._read_pane_turns(
                            lane, remaining, window=ACK_REREAD_WINDOW)
            except Exception as exc:
                if pending:
                    # The pane is the only evidence for a pending send; an
                    # unreadable one fails closed without ever retrying the send.
                    self._record(event_id, "RECONCILE_UNAVAILABLE")
                else:
                    # Observer failure never becomes ACKED; leave it unacked for next time.
                    self._note_ack_failure(rows, event_id, type(exc).__name__)
                continue
            texts = [_turn_text(turn) for turn in turns]
            if (any(_ack_candidate(text, event_id) for text in texts)
                    and _delivery_turn_index(turns, event_id) is None):
                # The delivery turn is still absent after the one wider read:
                # the reply is unproven, not a nonce-only settlement.
                if latest.get(event_id) != ACK_DELIVERY_UNPROVEN:
                    self._record(event_id, ACK_DELIVERY_UNPROVEN,
                                 {"lane": lane, "conversation_id": delivery["conv"]})
                continue
            matched = _ack_turn_index(turns, event_id)
            if matched is not None:
                ack_utc = _turn_utc(turns[matched])
                # Capability belongs to the READBACK, not to one turn: a pane
                # that never supplies wall-clock time cannot time-bind an ACK.
                readback_timed = any(_turn_utc(turn) is not None for turn in turns)
                if not readback_timed:
                    # Degrade, do not stall and do not claim time was verified:
                    # delivery anchor + turn order + nonce is the strongest
                    # binding this readback can supply.
                    digest = hashlib.sha256(texts[matched].encode("utf-8")).hexdigest()[:12]
                    self.record_assistant_ack(
                        event_id, message_ref="%d:%s" % (matched, digest),
                        matched_utc=None, lane=lane,
                        lane_source=delivery["source"],
                        conversation_id=delivery["conv"], generation=recorded_gen,
                        outcome=ACKED_ORDER_ONLY, ack_binding="order_only")
                    continue
                ack_stamp = _parse_utc(ack_utc)
                delivery_stamp = _parse_utc(delivery["utc"])
                if ack_stamp is None or delivery_stamp is None:
                    proof = ACK_TIME_UNPROVEN
                elif delivery_stamp - ack_stamp > timedelta(seconds=self.ack_skew_seconds):
                    proof = ACK_BEFORE_DELIVERY
                else:
                    proof = None
                if proof is not None:
                    if latest.get(event_id) != proof:
                        self._record(event_id, proof,
                                     {"delivery_utc": delivery["utc"], "ack_utc": ack_utc})
                else:
                    digest = hashlib.sha256(texts[matched].encode("utf-8")).hexdigest()[:12]
                    self.record_assistant_ack(
                        event_id, message_ref="%d:%s" % (matched, digest),
                        matched_utc=_utc_now(), lane=lane,
                        lane_source=delivery["source"],
                        conversation_id=delivery["conv"], generation=recorded_gen)
            elif (any(_ack_candidate(text, event_id) for text in texts)
                  and _delivery_turn_index(turns, event_id) is not None):
                # Delivery anchor is present but the ACK is not after it in
                # turn order: refuse explicitly (the only anchor available to
                # a timestamp-less readback is order).
                if latest.get(event_id) != ACK_BEFORE_DELIVERY:
                    self._record(event_id, ACK_BEFORE_DELIVERY,
                                 {"delivery_turn_present": True, "ack_after_delivery": False})
            elif pending and _delivery_present(texts, event_id):
                self._record(event_id, "DELIVERED_UNCONFIRMED")
            else:
                # A readable tail without the nonce proves nothing; a pending
                # event stays open instead of expiring into a terminal state.
                age = self._age_seconds(row)
                if not pending and age is not None and age > self.ack_horizon_seconds:
                    self._record(event_id, "UNACKED_EXPIRED")

    def _consecutive_ack_errors(self, rows, event_id):
        """Count the trailing run of ACK_CHECK_ERROR outcomes for one event."""
        count = 0
        for row in reversed(rows):
            if row.get("kind") != "outcome" or row.get("event_id") != event_id:
                continue
            outcome = row.get("outcome")
            if isinstance(outcome, str) and outcome.startswith("ACK_CHECK_ERROR:"):
                count += 1
            else:
                break
        return count

    def _note_ack_failure(self, rows, event_id, reason):
        """Record an observer failure, capping retries at ACK_MAX_ERRORS."""
        if self._consecutive_ack_errors(rows, event_id) >= ACK_MAX_ERRORS:
            self._record(event_id, "ACK_CHECK_GAVE_UP")
        else:
            self._record(event_id, "ACK_CHECK_ERROR:%s" % reason)

    # -- liveness ----------------------------------------------------------
    def _write_status(self):
        """Best-effort liveness record next to the journal. Never raises."""
        try:
            rows = self._rows()
            events = [row for row in rows if row.get("kind") == "event"]
            outcomes = [row for row in rows if row.get("kind") == "outcome"]
            latest = {}
            for row in outcomes:
                event_id = row.get("event_id")
                if event_id is not None:
                    latest[event_id] = row
            event_ids = {row.get("event_id") for row in events}
            acked = sum(1 for event_id in event_ids
                        if latest.get(event_id, {}).get("outcome") == "DELIVERED_ACKED")
            failed = sum(1 for event_id in event_ids
                         if self._is_failure_outcome(latest.get(event_id, {}).get("outcome")))
            last_event = events[-1] if events else None
            last_outcome = outcomes[-1] if outcomes else None
            record = {
                "last_event_utc": last_event.get("utc") if last_event else None,
                "last_outcome": last_outcome.get("outcome") if last_outcome else None,
                "last_outcome_utc": last_outcome.get("utc") if last_outcome else None,
                "total_events": len(events),
                "total_acked": acked,
                "total_failed": failed,
            }
            self._atomic_write_json(self.status_path, record)
        except BaseException:
            pass

    @staticmethod
    def _is_failure_outcome(outcome):
        return isinstance(outcome, str) and (
            outcome.startswith(("REFUSED:", "BLOCKED_", "DELIVERY_ERROR:", "LOCK_TIMEOUT",
                                "INTERNAL_ERROR:"))
            or outcome == "DELIVERED_BUSY")

    def _atomic_write_json(self, path, record):
        """Overwrite one JSON file atomically: temp file in the same dir, then replace."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                                dir=str(path.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(record, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _read_pane_turns(self, lane, timeout, window=None):
        argv = [self.python_executable, self.pane_read_path, lane, "--json"]
        if window is not None:
            argv += ["--window", str(window)]
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if completed.returncode != 0:
            raise subprocess.CalledProcessError(completed.returncode, argv,
                                                output=completed.stdout, stderr=completed.stderr)
        return _pane_turns(completed.stdout or "")

    def _age_seconds(self, row):
        raw = row.get("utc")
        if not raw:
            return None
        stamp = _parse_utc(raw)
        return None if stamp is None else (datetime.now(timezone.utc) - stamp).total_seconds()

    # -- delivery ----------------------------------------------------------
    def _sender_argv(self, origin, event_id, lane):
        # A bare nonce gives a real recipient nothing to act on, so the event
        # expires UNACKED. Carry an explicit, minimal reply instruction; each
        # form names the id exactly once.
        nonce = (
            f"EVENT-ID: {event_id}\n"
            f"WAKE from Hermes (hermes:mcp-integration). Reply with this exact line "
            f"and nothing else: WAKE-ACK {event_id}"
        )
        return [
            self.python_executable,
            self.sender_path,
            lane or "",
            nonce,
            "--expect-conv", str(origin["conv"]),
            "--expect-gen", str(origin["generation"]),
            "--read-response",
        ]

    def _run_sender(self, argv):
        # One physical submission per call; no retry loop here. The sender's
        # line contract decides the outcome: a REFUSED: line is a non-terminal
        # no-send, only a real success marker is a delivery. The wait is bounded
        # by the active hook deadline, so the hook returns inside its budget
        # instead of being abandoned by the host's 30 s callback timeout.
        deadline = getattr(self, "_sender_deadline", None)
        timeout = SENDER_TIMEOUT_SECONDS if deadline is None else max(
            0.0, deadline - time.monotonic())
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        return _classify_sender_output(output, completed.returncode)

    def _record(self, event_id, outcome, details=None):
        row = {"kind": "outcome", "event_id": event_id, "utc": _utc_now(), "outcome": outcome}
        if details:
            row.update(details)
        self._append(row)


def build_from_ctx(ctx):
    """Build an emitter from plugin settings; flag defaults to OFF."""
    getter = getattr(ctx, "get_config", None)

    def config(key, default=None):
        if callable(getter):
            try:
                value = getter(key, default)
            except Exception:
                return default
            return default if value is None else value
        return default

    # Defaults are deliberately fail-closed and absolute: an unconfigured install
    # has no lane, so resolve_origin refuses, and every path is anchored at the
    # Hermes home so an arbitrary CWD cannot redirect the journal or the sender.
    return WakeEmitter(
        journal_path=Path(config("journal_path", str(Path.home() / ".hermes" / "state" / "event_outbox.jsonl"))),
        peer_map_path=Path(config("peer_map_path", str(Path.home() / ".hermes" / "state" / "peer_lane_map.json"))),
        sender_path=config("sender_path", str(Path.home() / ".hermes" / "scripts" / "pane_send_guarded.py")),
        pane_read_path=config("pane_read_path", str(Path.home() / ".hermes" / "scripts" / "pane_read.py")),
        python_executable=config("python_executable", None),
        ack_horizon_seconds=config("ack_horizon_seconds", ACK_HORIZON_SECONDS),
        ack_skew_seconds=config("ack_skew_seconds", ACK_SKEW_SECONDS),
        hook_budget_seconds=config("hook_budget_seconds", HOOK_BUDGET_SECONDS),
        retry_horizon_seconds=config("retry_horizon_seconds", RETRY_HORIZON_SECONDS),
        binding_path=config("binding_path",
                            str(Path.home() / ".hermes" / "state" / "task_bindings.jsonl")),
        allow_unbound_global_fallback=bool(config("allow_unbound_global_fallback", False)),
        lane=config("lane", None),
        enabled=bool(config("enabled", False)),
        scope=config("scope", None),
        logger=getattr(ctx, "logger", None),
    )


def register(ctx):
    """Normal plugin entry point: register the single lifecycle hook."""
    worker = build_from_ctx(ctx)
    ctx.register_hook("on_session_end", worker.on_session_end)
    return worker
