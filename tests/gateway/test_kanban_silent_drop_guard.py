"""D-ROLLOVER: a saved local session id is a hint, validated against state.db at delivery time.

History: 2026-11-04 all 178 kanban_notify_subs rows pointed at a dead desktop session; sends were
accepted, cursors advanced, events vanished. A first fix rebound a dead target to the NEWEST live
session of the same source -- which is wrong (it picks subagents / unrelated agents). The rule
enforced here:

* saved id live                      -> deliver to it, nothing changes
* saved id ended by a rollover       -> follow parent_session_id -> child chain; accept ONLY a
  (end_reason 'compression')            single same-source, same-profile child per hop
* ambiguous / no successor / unknown -> fail closed: log, rewind the cursor, event stays pending
* never select by recency; never touch the persisted subscription row (cursor ops keep self.sub)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import pathlib
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.kanban_watchers_notifier import (
    _KanbanNotification,
    resolve_live_local_session,
)


def _db(sessions: list[dict]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, profile_name TEXT, "
        "parent_session_id TEXT, started_at REAL, ended_at REAL, end_reason TEXT, chat_id TEXT)"
    )
    for i, s in enumerate(sessions):
        conn.execute(
            "INSERT INTO sessions (id, source, profile_name, parent_session_id, started_at, ended_at, end_reason, chat_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (s["id"], s.get("source", "local"), s.get("profile", "default"), s.get("parent"),
             s.get("started", float(i)), s.get("ended"), s.get("reason"), s.get("chat_id")),
        )
    conn.commit()
    return conn


def _sess(id, **kw):
    return {"id": id, **kw}


# ---------------------------------------------------------------- resolver

def test_live_saved_id_is_used_as_is():
    c = _db([_sess("a")])
    assert resolve_live_local_session(c, "a", "local") == ("live", "a", "")


def test_single_hop_rollover_rebinds_to_child():
    c = _db([_sess("a", ended=1.0, reason="compression"), _sess("b", parent="a")])
    assert resolve_live_local_session(c, "a", "local") == ("rebound", "b", "")


def test_multi_hop_rollover_follows_the_chain_to_the_live_leaf():
    c = _db([
        _sess("a", ended=1.0, reason="compression"),
        _sess("b", parent="a", ended=2.0, reason="compression"),
        _sess("c", parent="b", ended=3.0, reason="compression"),
        _sess("d", parent="c"),
    ])
    assert resolve_live_local_session(c, "a", "local")[:2] == ("rebound", "d")


def test_subagent_children_are_never_the_successor():
    """Real shape: a rolled-over runner has subagent children plus ONE local child."""
    c = _db([
        _sess("run1", ended=1.0, reason="compression"),
        _sess("sub1", source="subagent", parent="run1"),  # live subagent, must be ignored
        _sess("run2", parent="run1"),
    ])
    assert resolve_live_local_session(c, "run1", "local")[:2] == ("rebound", "run2")


def test_only_subagent_children_means_no_successor():
    c = _db([_sess("run1", ended=1.0, reason="compression"), _sess("sub1", source="subagent", parent="run1")])
    status, target, why = resolve_live_local_session(c, "run1", "local")
    assert (status, target) == ("failed", None) and "no same-source successor" in why


def test_newest_live_session_is_never_chosen_by_recency():
    """An unrelated newer live session of the same source must not receive the event."""
    c = _db([
        _sess("dead", ended=1.0, reason="ws_orphan_reap", started=1.0),
        _sess("unrelated_newest", started=99.0),
    ])
    assert resolve_live_local_session(c, "dead", "local")[:2] == ("failed", None)


def test_dead_without_rollover_reason_does_not_follow_children():
    """ws_orphan_reap / agent_close are not rollovers even if a child row exists."""
    c = _db([_sess("a", ended=1.0, reason="ws_orphan_reap"), _sess("b", parent="a")])
    status, target, why = resolve_live_local_session(c, "a", "local")
    assert (status, target) == ("failed", None) and "not a rollover" in why


def test_two_same_source_children_is_ambiguous_and_fails():
    c = _db([_sess("a", ended=1.0, reason="compression"), _sess("b", parent="a"), _sess("c", parent="a")])
    status, target, why = resolve_live_local_session(c, "a", "local")
    assert (status, target) == ("failed", None) and "ambiguous" in why


def test_child_of_a_different_profile_is_not_a_successor():
    c = _db([_sess("a", ended=1.0, reason="compression"), _sess("b", parent="a", profile="other")])
    assert resolve_live_local_session(c, "a", "local")[:2] == ("failed", None)


def test_chain_ending_dead_fails_closed():
    c = _db([
        _sess("a", ended=1.0, reason="compression"),
        _sess("b", parent="a", ended=2.0, reason="ws_orphan_reap"),
    ])
    assert resolve_live_local_session(c, "a", "local")[:2] == ("failed", None)


def test_unknown_saved_id_fails_closed():
    c = _db([_sess("x")])
    status, target, why = resolve_live_local_session(c, "nope", "local")
    assert (status, target) == ("failed", None) and "not found" in why


def test_saved_chat_id_matching_two_sessions_is_ambiguous():
    c = _db([_sess("a", chat_id="room"), _sess("b", chat_id="room")])
    assert resolve_live_local_session(c, "room", "local")[:2] == ("failed", None)


def test_chat_id_fallback_when_unique():
    c = _db([_sess("a", chat_id="room")])
    assert resolve_live_local_session(c, "room", "local") == ("live", "room", "")


def test_platform_source_mismatch_fails():
    c = _db([_sess("a", source="desktop", ended=1.0, reason="compression"), _sess("b", source="desktop", parent="a")])
    assert resolve_live_local_session(c, "a", "local")[:2] == ("failed", None)


def test_rebind_uses_successor_chat_id_when_set():
    c = _db([_sess("a", ended=1.0, reason="compression"), _sess("b", parent="a", chat_id="chat-b")])
    assert resolve_live_local_session(c, "a", "local")[:2] == ("rebound", "chat-b")


def test_rollover_cycle_terminates():
    c = _db([_sess("a", parent="b", ended=1.0, reason="compression"), _sess("b", parent="a", ended=1.0, reason="compression")])
    assert resolve_live_local_session(c, "a", "local")[:2] == ("failed", None)


# ---------------------------------------------------------------- guard + deliver wiring

def _file_db(tmp_path: pathlib.Path, sessions: list[dict]) -> pathlib.Path:
    path = tmp_path / "state.db"
    src = _db(sessions)
    dst = sqlite3.connect(path)
    src.backup(dst)
    dst.close()
    return path


def _notification(sub: dict, db_path) -> _KanbanNotification:
    n = object.__new__(_KanbanNotification)
    n.sub = sub
    n._delivery_sub = None
    n._dead_recipient = False
    n.task_id = sub["task_id"]
    n.platform_str = sub["platform"]
    n._state_db_path = lambda: db_path
    return n


def _sub(chat_id="a", platform="local"):
    return {"task_id": "t_1", "platform": platform, "chat_id": chat_id, "thread_id": ""}


def test_guard_rebinds_delivery_view_but_not_the_persisted_sub(tmp_path):
    db = _file_db(tmp_path, [_sess("a", ended=1.0, reason="compression"), _sess("b", parent="a")])
    sub = _sub("a")
    n = _notification(sub, db)
    n._guard_stale_local_session()
    assert n._dead_recipient is False
    assert n.dsub["chat_id"] == "b"
    assert sub["chat_id"] == "a", "cursor ops key on the persisted row; it must not be mutated"
    assert n.dsub is not sub


def test_guard_live_target_unchanged(tmp_path):
    db = _file_db(tmp_path, [_sess("a")])
    n = _notification(_sub("a"), db)
    n._guard_stale_local_session()
    assert n._dead_recipient is False and n.dsub["chat_id"] == "a"


def test_guard_ignores_non_local_platforms(tmp_path):
    db = _file_db(tmp_path, [])
    n = _notification(_sub("12345", platform="telegram"), db)
    n._guard_stale_local_session()
    assert n._dead_recipient is False and n.dsub["chat_id"] == "12345"


def test_guard_fails_closed_and_logs_when_ambiguous(tmp_path, caplog):
    db = _file_db(tmp_path, [_sess("a", ended=1.0, reason="compression"), _sess("b", parent="a"), _sess("c", parent="a")])
    n = _notification(_sub("a"), db)
    _KanbanNotification._DEAD_RECIPIENT_LOGGED.clear()
    with caplog.at_level(logging.WARNING):
        n._guard_stale_local_session()
    assert n._dead_recipient is True
    assert any("D-ROLLOVER FAILED" in r.getMessage() and "ambiguous" in r.getMessage() for r in caplog.records)


def test_guard_fails_closed_when_state_db_missing(tmp_path):
    n = _notification(_sub("a"), tmp_path / "missing.db")
    n._guard_stale_local_session()
    assert n._dead_recipient is True


def test_guard_fails_closed_when_state_db_path_unresolvable():
    n = _notification(_sub("a"), None)
    n._guard_stale_local_session()
    assert n._dead_recipient is True


def test_state_db_is_opened_read_only(tmp_path):
    db = _file_db(tmp_path, [_sess("a", ended=1.0, reason="compression"), _sess("b", parent="a")])
    before = db.read_bytes()
    _notification(_sub("a"), db)._guard_stale_local_session()
    assert db.read_bytes() == before


def _deliver_harness(tmp_path, sessions, chat_id="a"):
    db = _file_db(tmp_path, sessions)
    sub = _sub(chat_id)
    n = _notification(sub, db)
    n.d = {"sub": sub, "events": [], "cursor": 5, "old_cursor": 1}
    n.platform_cls = lambda v: v
    n.rewind = AsyncMock()
    n.advance = AsyncMock()
    n.unsub = AsyncMock()
    n.runner = MagicMock()
    seen = {}

    async def fake_send_pings():
        seen["ping_chat"] = n.dsub["chat_id"]
        return True

    n._send_pings = fake_send_pings
    n._owner_scope = lambda: contextlib.nullcontext()
    n.sub_profile = ""
    n.wake_kinds = set()
    n.is_push_adapter = True
    n.wake_agent = False
    n.send_passive = True
    n.task = None
    return n, sub, seen


def _run_deliver(n):
    with patch("gateway.kanban_watchers_notifier._adapter_for_subscription", return_value=MagicMock()), \
            patch("gateway.wake.adapter_supports_push", return_value=True), \
            patch("gateway.warning_notifications.warning_notifications_enabled", return_value=True):
        asyncio.run(n.deliver())


def test_deliver_rewinds_and_never_advances_when_recipient_unresolvable(tmp_path):
    n, _, seen = _deliver_harness(tmp_path, [_sess("a", ended=1.0, reason="ws_orphan_reap"), _sess("newest")])
    _run_deliver(n)
    n.rewind.assert_awaited_once()
    n.advance.assert_not_awaited()
    n.unsub.assert_not_awaited()  # subscription is kept; the event stays pending
    assert "ping_chat" not in seen, "nothing may be sent to a recipient that failed validation"


def test_deliver_to_rolled_over_recipient_uses_successor_and_advances_original_row(tmp_path):
    n, sub, seen = _deliver_harness(tmp_path, [_sess("a", ended=1.0, reason="compression"), _sess("b", parent="a")])
    _run_deliver(n)
    assert seen["ping_chat"] == "b"
    n.advance.assert_awaited_once()
    n.rewind.assert_not_awaited()
    assert sub["chat_id"] == "a"
