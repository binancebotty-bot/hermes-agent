"""Integration test for D-ROLLOVER fix: demonstrates requirements (a) through (e).

This test verifies:
(a) A subscription whose chat_id is a dead/unknown session is NOT advanced in the cursor, and the failure is recorded
(b) After a simulated rollover to a new live session, delivery targets the NEW session without any manual edit
(c) A live session still receives normally (no regression)
(d) Rows for other platforms/agents are untouched by the rollover migration
(e) The guard never blocks delivery of a genuinely live recipient, and never raises into the notifier loop
"""

from __future__ import annotations

import asyncio
import pathlib
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.kanban_watchers_notifier import _KanbanNotification
from gateway.kanban_watchers_notifier import _notifier_collect
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from gateway.config import Platform


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


def _create_notification(runner, sub: dict, state_db_path: pathlib.Path, events: list, task=None) -> _KanbanNotification:
    """Create a _KanbanNotification instance with mocked dependencies."""
    n = _KanbanNotification.__new__(_KanbanNotification)
    n.runner = runner
    n.d = {"sub": sub, "events": events, "task": task, "board": "default"}
    n.platform_str = sub["platform"]
    n.task_id = sub["task_id"]
    n.sub = sub
    n.board_slug = "default"
    n.sub_profile = sub.get("notifier_profile") or ""
    n.title = task.title if task else sub["task_id"]
    n.board_tag = ""
    n.head = f"[{sub['task_id']}]"
    n.sub_key = (sub["task_id"], sub["platform"], sub["chat_id"], sub.get("thread_id") or "")
    mode = sub.get("delivery_mode") or "notify"
    n.wake_agent = mode in ("notify+wake", "wake")
    n.send_passive = mode != "wake"
    n.wake_handoff = n.wake_review_detail = n.session_key = n.synth = ""
    n.plat = Platform(sub["platform"])
    n.adapter = AsyncMock()
    n.adapter.send = AsyncMock(return_value=MagicMock(success=True))
    n.is_push_adapter = True
    n.wake_kinds = set()
    n.sub_fail_counts = {}
    n._get_profile_state_db_path = MagicMock(return_value=state_db_path)
    return n


class MockRunner:
    """Mock GatewayRunner for testing."""
    def __init__(self, state_db_path: pathlib.Path):
        self.config = None
        self._kanban_notifier_profile = "default"
        self._primary_profile_name = "default"
        self._active_profile_name = lambda: "default"
        self.adapters = {}
        self._profile_adapters = {}
        self._state_db_path = state_db_path

    def _authorization_adapter(self, platform, owner_profile):
        return self.adapters.get(platform)

    def _owns_kanban_dispatcher_lock(self):
        return True

    def _active_profile_name(self):
        return "default"

    async def _kanban_advance(self, sub, cursor, board=None):
        pass

    async def _kanban_rewind(self, sub, claimed_cursor, old_cursor, board=None):
        pass

    async def _kanban_unsub(self, sub, board=None):
        pass

    async def _deliver_kanban_artifacts(self, *, adapter, chat_id, metadata, event_payload, task):
        pass

    def _resolve_profile_home_for_source(self, source):
        from contextlib import nullcontext
        return nullcontext()


class MockTask:
    def __init__(self, task_id, title, assignee="worker", status="completed", session_id=""):
        self.task_id = task_id
        self.title = title
        self.assignee = assignee
        self.status = status
        self.session_id = session_id
        self.result = "Test result"


@pytest.mark.asyncio
async def test_a_dead_session_not_advanced_cursor_recorded_failure(tmp_path):
    """(a) Dead session -> cursor NOT advanced, failure recorded."""
    # State.db: target session is DEAD, no live replacement
    state_db = _create_test_state_db(tmp_path, [
        {"id": "20261103_114419_6cd0d4", "source": "desktop", "started_at": 1000.0, "ended_at": 2000.0, "end_reason": "compression", "chat_id": "20261103_114419_6cd0d4"},
    ])

    runner = MockRunner(state_db)
    sub = {"task_id": "t_dead", "platform": "desktop", "chat_id": "20261103_114419_6cd0d4", "thread_id": "", "delivery_mode": "notify+wake"}
    task = MockTask("t_dead", "Dead session test")
    events = [MagicMock(kind="completed", id=1, payload={"summary": "done"})]

    n = _create_notification(runner, sub, state_db, events, task)
    n._guard_stale_local_session()

    # Should mark as dead recipient
    assert getattr(n, "_dead_recipient", False) is True
    assert ("desktop", "20261103_114419_6cd0d4", "t_dead") in _KanbanNotification._DEAD_RECIPIENT_LOGGED

    # When deliver() is called, it should rewind not advance
    n.adapter = AsyncMock()
    n.adapter.send = AsyncMock(return_value=MagicMock(success=True))
    n.plat = Platform("desktop")
    n.is_push_adapter = True

    # Mock the rewind and advance to track which is called
    n.rewind = AsyncMock()
    n.advance = AsyncMock()

    await n.deliver()

    # Should call rewind, not advance
    n.rewind.assert_called_once()
    n.advance.assert_not_called()


@pytest.mark.asyncio
async def test_b_rollover_to_new_live_session_delivers_to_new(tmp_path):
    """(b) Simulated rollover -> delivery targets NEW session without manual edit."""
    # State.db: old session DEAD, new session LIVE
    state_db = _create_test_state_db(tmp_path, [
        {"id": "20261103_114419_6cd0d4", "source": "desktop", "started_at": 1000.0, "ended_at": 2000.0, "end_reason": "compression", "chat_id": "20261103_114419_6cd0d4"},
        {"id": "20261104_092959_5eadd7", "source": "desktop", "started_at": 3000.0, "ended_at": None, "chat_id": "20261104_092959_5eadd7"},
    ])

    runner = MockRunner(state_db)
    sub = {"task_id": "t_rollover", "platform": "desktop", "chat_id": "20261103_114419_6cd0d4", "thread_id": "", "delivery_mode": "notify+wake"}
    task = MockTask("t_rollover", "Rollover test")
    events = [MagicMock(kind="completed", id=1, payload={"summary": "done"})]

    n = _create_notification(runner, sub, state_db, events, task)
    n._guard_stale_local_session()

    # Should rebind to live session
    assert sub["chat_id"] == "20261104_092959_5eadd7"
    assert not getattr(n, "_dead_recipient", False)
    assert ("desktop", "20261103_114419_6cd0d4", "20261104_092959_5eadd7") in _KanbanNotification._REBOUND_LOGGED

    # Delivery should proceed normally (advance cursor)
    n.adapter = AsyncMock()
    n.adapter.send = AsyncMock(return_value=MagicMock(success=True))
    n.plat = Platform("desktop")
    n.is_push_adapter = True
    n.rewind = AsyncMock()
    n.advance = AsyncMock()

    await n.deliver()

    # Should call advance (delivery succeeded)
    n.advance.assert_called_once()
    n.rewind.assert_not_called()


@pytest.mark.asyncio
async def test_c_live_session_receives_normally_no_regression(tmp_path):
    """(c) Live session still receives normally (no regression)."""
    # State.db: target session is LIVE
    state_db = _create_test_state_db(tmp_path, [
        {"id": "20261103_114419_6cd0d4", "source": "desktop", "started_at": 1000.0, "ended_at": None, "chat_id": "20261103_114419_6cd0d4"},
    ])

    runner = MockRunner(state_db)
    sub = {"task_id": "t_live", "platform": "desktop", "chat_id": "20261103_114419_6cd0d4", "thread_id": "", "delivery_mode": "notify+wake"}
    task = MockTask("t_live", "Live session test")
    events = [MagicMock(kind="completed", id=1, payload={"summary": "done"})]

    n = _create_notification(runner, sub, state_db, events, task)
    n._guard_stale_local_session()

    # Should NOT mark as dead, NOT rebind
    assert sub["chat_id"] == "20261103_114419_6cd0d4"
    assert not getattr(n, "_dead_recipient", False)
    assert ("desktop", "20261103_114419_6cd0d4", "t_live") not in _KanbanNotification._DEAD_RECIPIENT_LOGGED

    # Delivery should proceed normally
    n.adapter = AsyncMock()
    n.adapter.send = AsyncMock(return_value=MagicMock(success=True))
    n.plat = Platform("desktop")
    n.is_push_adapter = True
    n.rewind = AsyncMock()
    n.advance = AsyncMock()

    await n.deliver()

    n.advance.assert_called_once()
    n.rewind.assert_not_called()


@pytest.mark.asyncio
async def test_d_other_platforms_untouched(tmp_path):
    """(d) Rows for other platforms (tui, agent:main:local:dm) are untouched."""
    # State.db: only desktop has live session
    state_db = _create_test_state_db(tmp_path, [
        {"id": "20261104_092959_5eadd7", "source": "desktop", "started_at": 3000.0, "ended_at": None, "chat_id": "20261104_092959_5eadd7"},
    ])

    runner = MockRunner(state_db)

    # Test TUI platform - should NOT be affected by desktop sessions
    sub_tui = {"task_id": "t_tui", "platform": "tui", "chat_id": "some_tui_session", "thread_id": "", "delivery_mode": "notify+wake"}
    task = MockTask("t_tui", "TUI test")
    events = [MagicMock(kind="completed", id=1, payload={"summary": "done"})]

    n = _create_notification(runner, sub_tui, state_db, events, task)
    n._guard_stale_local_session()

    # TUI target unchanged (no live tui session in state.db -> fail closed)
    assert sub_tui["chat_id"] == "some_tui_session"
    assert getattr(n, "_dead_recipient", False) is True
    assert ("tui", "some_tui_session", "t_tui") in _KanbanNotification._DEAD_RECIPIENT_LOGGED

    # Test telegram platform - should be ignored entirely
    sub_tg = {"task_id": "t_tg", "platform": "telegram", "chat_id": "12345", "thread_id": "", "delivery_mode": "notify+wake"}
    n2 = _create_notification(runner, sub_tg, state_db, events, task)
    n2._guard_stale_local_session()

    # Telegram target unchanged (not a local platform)
    assert sub_tg["chat_id"] == "12345"
    assert not getattr(n2, "_dead_recipient", False)


@pytest.mark.asyncio
async def test_e_guard_never_blocks_live_recipient_never_raises(tmp_path):
    """(e) Guard never blocks genuinely live recipient, never raises into notifier loop."""
    # State.db: target session is LIVE
    state_db = _create_test_state_db(tmp_path, [
        {"id": "20261103_114419_6cd0d4", "source": "desktop", "started_at": 1000.0, "ended_at": None, "chat_id": "20261103_114419_6cd0d4"},
    ])

    runner = MockRunner(state_db)
    sub = {"task_id": "t_e", "platform": "desktop", "chat_id": "20261103_114419_6cd0d4", "thread_id": "", "delivery_mode": "notify+wake"}
    task = MockTask("t_e", "Live test")
    events = [MagicMock(kind="completed", id=1, payload={"summary": "done"})]

    n = _create_notification(runner, sub, state_db, events, task)

    # Guard should not raise, should not mark dead
    n._guard_stale_local_session()
    assert not getattr(n, "_dead_recipient", False)

    # Full delivery should not raise
    n.adapter = AsyncMock()
    n.adapter.send = AsyncMock(return_value=MagicMock(success=True))
    n.plat = Platform("desktop")
    n.is_push_adapter = True
    n.rewind = AsyncMock()
    n.advance = AsyncMock()
    n.wake = AsyncMock()

    await n.deliver()

    # Should complete without raising
    n.advance.assert_called_once()
    n.rewind.assert_not_called()
    n.wake.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])