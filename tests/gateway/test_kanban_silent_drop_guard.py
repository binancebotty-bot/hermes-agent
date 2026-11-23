"""Silent-drop guard: a local-platform notification aimed at a dead session id.

Regression context (2026-11-04): all 178 kanban_notify_subs rows pointed at a desktop
session from the previous day. The adapter accepted each send without raising, so the
cursor advanced, the event was consumed, and every crash / quota wall / rc=0-without-
complete reached nobody -- no failure counter, no log, no retry.

``_guard_stale_local_session`` validates the target against the authoritative sessions
table (state.db) at DELIVERY TIME. A stored chat_id is a HINT that is validated, never
trusted blindly. FAIL CLOSED on a dead recipient: if no live session exists for this
platform/profile, rewind the cursor, log the failure, and increment the failure counter.
"""

from __future__ import annotations

import pathlib
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

from gateway.kanban_watchers_notifier import _KanbanNotification


def _create_test_state_db(tmp_path: pathlib.Path, sessions: list[dict]) -> pathlib.Path:
    """Create a test state.db with the sessions table populated."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                started_at REAL NOT NULL,
                ended_at REAL,
                end_reason TEXT,
                chat_id TEXT,
                profile_name TEXT
            )
        """)
        for s in sessions:
            conn.execute(
                "INSERT INTO sessions (id, source, started_at, ended_at, end_reason, chat_id, profile_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (s["id"], s["source"], s["started_at"], s.get("ended_at"), s.get("end_reason"), s.get("chat_id"), s.get("profile_name", "default")),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _notification(sub: dict, *, push: bool = True, state_db_path: pathlib.Path = None) -> _KanbanNotification:
    """Bare instance: only the attributes the guard touches."""
    n = object.__new__(_KanbanNotification)
    n.sub = sub
    n.platform_str = (sub.get("platform") or "").lower()
    n.task_id = sub["task_id"]
    n.session_key = ""
    n.sub_key = (sub["task_id"], sub["platform"], sub["chat_id"], sub.get("thread_id") or "")
    n.is_push_adapter = push
    n.sub_fail_counts = {}
    n.runner = MagicMock()
    n.runner.config = None
    n._kanban_notifier_profile = "default"
    n._primary_profile_name = "default"
    n._active_profile_name = lambda: "default"
    # Mock the profile state.db path resolution
    if state_db_path:
        n._get_profile_state_db_path = MagicMock(return_value=state_db_path)
    return n


def _sub(platform="desktop", chat_id="20261103_114419_6cd0d4") -> dict:
    return {"task_id": "t_1", "platform": platform, "chat_id": chat_id, "thread_id": ""}


def test_rebinds_dead_session_onto_live_session(tmp_path):
    """Dead target is rebound to live session from state.db."""
    # Create state.db with one DEAD session (the target) and one LIVE session
    state_db = _create_test_state_db(tmp_path, [
        {"id": "20261103_114419_6cd0d4", "source": "desktop", "started_at": 1000.0, "ended_at": 2000.0, "end_reason": "compression", "chat_id": "20261103_114419_6cd0d4"},
        {"id": "20261104_092959_5eadd7", "source": "desktop", "started_at": 3000.0, "ended_at": None, "chat_id": "20261104_092959_5eadd7"},
    ])
    sub = _sub(chat_id="20261103_114419_6cd0d4")  # dead target
    n = _notification(sub, state_db_path=state_db)

    n._guard_stale_local_session()

    # Should rebind to the live session
    assert sub["chat_id"] == "20261104_092959_5eadd7"
    assert n.sub_key[2] == "20261104_092959_5eadd7"
    assert n.session_key == "20261104_092959_5eadd7"
    assert ("desktop", "20261103_114419_6cd0d4", "20261104_092959_5eadd7") in _KanbanNotification._REBOUND_LOGGED
    assert not getattr(n, "_dead_recipient", False)


def test_leaves_a_live_target_alone(tmp_path):
    """Live target is not modified."""
    state_db = _create_test_state_db(tmp_path, [
        {"id": "20261103_114419_6cd0d4", "source": "desktop", "started_at": 1000.0, "ended_at": None, "chat_id": "20261103_114419_6cd0d4"},
        {"id": "20261104_092959_5eadd7", "source": "desktop", "started_at": 2000.0, "ended_at": 3000.0, "end_reason": "compression", "chat_id": "20261104_092959_5eadd7"},
    ])
    sub = _sub(chat_id="20261103_114419_6cd0d4")  # live target
    n = _notification(sub, state_db_path=state_db)

    n._guard_stale_local_session()

    assert sub["chat_id"] == "20261103_114419_6cd0d4"
    assert not getattr(n, "_dead_recipient", False)


def test_ignores_non_local_platforms(tmp_path):
    """Non-local platforms (telegram, etc.) are never rewritten."""
    state_db = _create_test_state_db(tmp_path, [
        {"id": "20261104_092959_5eadd7", "source": "desktop", "started_at": 3000.0, "ended_at": None, "chat_id": "20261104_092959_5eadd7"},
    ])
    sub = _sub(platform="telegram", chat_id="12345")
    n = _notification(sub, state_db_path=state_db)

    n._guard_stale_local_session()

    assert sub["chat_id"] == "12345"
    assert not getattr(n, "_dead_recipient", False)


def test_no_live_session_fails_closed(tmp_path):
    """No live session for platform -> fail closed (rewind cursor)."""
    state_db = _create_test_state_db(tmp_path, [
        {"id": "20261103_114419_6cd0d4", "source": "desktop", "started_at": 1000.0, "ended_at": 2000.0, "end_reason": "compression", "chat_id": "20261103_114419_6cd0d4"},
        # No live desktop session
    ])
    sub = _sub(chat_id="20261103_114419_6cd0d4")
    n = _notification(sub, state_db_path=state_db)

    n._guard_stale_local_session()

    assert sub["chat_id"] == "20261103_114419_6cd0d4"  # unchanged
    assert getattr(n, "_dead_recipient", False) is True
    assert ("desktop", "20261103_114419_6cd0d4", "t_1") in _KanbanNotification._DEAD_RECIPIENT_LOGGED


def test_picks_the_newest_live_session(tmp_path):
    """Multiple live sessions -> picks the newest (most recent started_at)."""
    state_db = _create_test_state_db(tmp_path, [
        {"id": "20261101_000000_aaaaaa", "source": "desktop", "started_at": 1000.0, "ended_at": None, "chat_id": "20261101_000000_aaaaaa"},
        {"id": "20261104_092959_5eadd7", "source": "desktop", "started_at": 3000.0, "ended_at": None, "chat_id": "20261104_092959_5eadd7"},
        {"id": "20261103_114419_6cd0d4", "source": "desktop", "started_at": 500.0, "ended_at": 2000.0, "end_reason": "compression", "chat_id": "20261103_114419_6cd0d4"},
    ])
    sub = _sub(chat_id="20261103_114419_6cd0d4")  # dead target
    n = _notification(sub, state_db_path=state_db)

    n._guard_stale_local_session()

    # Should pick the newest live session (20261104_092959_5eadd7)
    assert sub["chat_id"] == "20261104_092959_5eadd7"
    assert not getattr(n, "_dead_recipient", False)


def test_applies_to_all_local_platforms_not_just_push(tmp_path):
    """Guard applies to desktop/tui/local regardless of push adapter status."""
    state_db = _create_test_state_db(tmp_path, [
        {"id": "20261103_114419_6cd0d4", "source": "desktop", "started_at": 1000.0, "ended_at": 2000.0, "end_reason": "compression", "chat_id": "20261103_114419_6cd0d4"},
        {"id": "20261104_092959_5eadd7", "source": "desktop", "started_at": 3000.0, "ended_at": None, "chat_id": "20261104_092959_5eadd7"},
    ])
    sub = _sub(chat_id="20261103_114419_6cd0d4")
    # Test with push=False (non-push adapter like api_server)
    n = _notification(sub, push=False, state_db_path=state_db)

    n._guard_stale_local_session()

    # Should still rebind even for non-push adapters
    assert sub["chat_id"] == "20261104_092959_5eadd7"
    assert not getattr(n, "_dead_recipient", False)


def test_tui_platform_uses_tui_source(tmp_path):
    """TUI platform maps to 'tui' source in sessions table."""
    state_db = _create_test_state_db(tmp_path, [
        {"id": "local-old", "source": "tui", "started_at": 1000.0, "ended_at": 2000.0, "end_reason": "ws_orphan_reap", "chat_id": "local-old"},
        {"id": "local-new", "source": "tui", "started_at": 3000.0, "ended_at": None, "chat_id": "local-new"},
    ])
    sub = _sub(platform="tui", chat_id="local-old")
    n = _notification(sub, state_db_path=state_db)

    n._guard_stale_local_session()

    assert sub["chat_id"] == "local-new"
    assert not getattr(n, "_dead_recipient", False)


def test_local_platform_uses_local_source(tmp_path):
    """Local platform maps to 'local' source in sessions table."""
    state_db = _create_test_state_db(tmp_path, [
        {"id": "local-old", "source": "local", "started_at": 1000.0, "ended_at": 2000.0, "end_reason": "compression", "chat_id": "local-old"},
        {"id": "local-new", "source": "local", "started_at": 3000.0, "ended_at": None, "chat_id": "local-new"},
    ])
    sub = _sub(platform="local", chat_id="local-old")
    n = _notification(sub, state_db_path=state_db)

    n._guard_stale_local_session()

    assert sub["chat_id"] == "local-new"
    assert not getattr(n, "_dead_recipient", False)


def test_fallback_to_session_id_when_chat_id_null(tmp_path):
    """If live session has NULL chat_id, use session id as fallback."""
    state_db = _create_test_state_db(tmp_path, [
        {"id": "20261103_114419_6cd0d4", "source": "desktop", "started_at": 1000.0, "ended_at": 2000.0, "end_reason": "compression", "chat_id": "20261103_114419_6cd0d4"},
        {"id": "20261104_092959_5eadd7", "source": "desktop", "started_at": 3000.0, "ended_at": None, "chat_id": None},  # NULL chat_id
    ])
    sub = _sub(chat_id="20261103_114419_6cd0d4")
    n = _notification(sub, state_db_path=state_db)

    n._guard_stale_local_session()

    # Should fallback to session id when chat_id is NULL
    assert sub["chat_id"] == "20261104_092959_5eadd7"
    assert not getattr(n, "_dead_recipient", False)


def test_cannot_resolve_state_db_fails_closed(tmp_path):
    """If profile state.db cannot be resolved, fail closed."""
    sub = _sub(chat_id="20261103_114419_6cd0d4")
    n = _notification(sub, state_db_path=None)  # No state.db path
    # Explicitly mock to return None
    n._get_profile_state_db_path = MagicMock(return_value=None)

    n._guard_stale_local_session()

    assert getattr(n, "_dead_recipient", False) is True
    assert sub["chat_id"] == "20261103_114419_6cd0d4"  # unchanged
