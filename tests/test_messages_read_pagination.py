"""Contract tests for the mcp-serve bridge's ``messages_read`` ordering and cursor paging.

Two contract layers:

* The live-plugin specification (16 tests, names kept exactly): cursor paging in
  both directions with chronological pages, named errors, foreign-session refusal,
  and a read-only cursor-ownership lookup.
* The owner-approved newest-first read: the no-cursor default is newest-first,
  ``order`` selects the legacy chronological presentation, and oversized limits
  are refused rather than silently clamped.

Fake data only: ``_conversation_messages`` is stubbed with an in-memory list and
``_cursor_message_session`` with an in-memory ownership map.

RED first on the unchanged plugin (oldest-first default, no cursor, clamped limit);
GREEN once the read contract is implemented.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_bridge():
    path = Path(__file__).resolve().parents[1] / "bridge.py"
    spec = importlib.util.spec_from_file_location("mcp_serve_read_bridge_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses need a registered module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def subject(monkeypatch):
    bridge = _load_bridge()
    # id 102 is a tool message: never visible to a reader.
    rows = [
        {"id": 101, "role": "user", "content": "one", "timestamp": 1},
        {"id": 102, "role": "tool", "content": "hidden", "timestamp": 2},
        {"id": 104, "role": "assistant", "content": "two", "timestamp": 3},
        {"id": 109, "role": "user", "content": "three", "timestamp": 4},
        {"id": 113, "role": "assistant", "content": "four", "timestamp": 5},
        {"id": 130, "role": "user", "content": "five", "timestamp": 6},
    ]
    monkeypatch.setattr(bridge, "_conversation_messages", lambda key: (rows, None))
    monkeypatch.setattr(bridge, "_load_sessions_index", lambda: {"demo": {"session_id": "session-1"}})
    # Global message-id -> session-id ownership. 102 is this session's tool message,
    # 777 is unknown, 999 belongs to another session.
    ownership = {102: "session-1", 777: None, 999: "session-2"}
    # raising=False lets the restored tests demonstrate RED while the helper is absent.
    monkeypatch.setattr(bridge, "_cursor_message_session",
                        lambda message_id: (ownership.get(message_id), None), raising=False)
    return bridge, bridge._ToolHandlers(bridge=None), rows


def _ids(response):
    return [m["id"] for m in json.loads(response)["messages"]]


ALL_VISIBLE = ["130", "113", "109", "104", "101"]

# A walk that ignores its cursor would otherwise loop forever and hang the suite. Fail fast instead.
MAX_WALK_PAGES = 50


def _walk(handler, page_request, cursor=None):
    """Collect message ids across cursor pages; trip the cap if the cursor stops advancing."""
    collected, pages = [], 0
    while True:
        pages += 1
        assert pages <= MAX_WALK_PAGES, (
            f"walk exceeded {MAX_WALK_PAGES} pages - cursor is probably being ignored")
        page = json.loads(handler.messages_read("demo", **page_request(cursor)))
        collected += [m["id"] for m in page["messages"]]
        cursor = page["next_cursor"]
        if cursor is None:
            return collected


# --- Live-plugin specification (16 tests; names exact, none renamed) ---------


def test_red_before_id_is_supported_and_returns_immediately_older(subject):
    _bridge, handler, _rows = subject
    assert _ids(handler.messages_read("demo", limit=2, before_id="113")) == ["104", "109"]


def test_after_id_returns_first_newer_page_not_newest_page(subject):
    _bridge, handler, _rows = subject
    assert _ids(handler.messages_read("demo", limit=2, after_id="104")) == ["109", "113"]


def test_both_cursors_are_named_error(subject):
    _bridge, handler, _rows = subject
    assert json.loads(handler.messages_read("demo", before_id="109", after_id="113"))["error"] == "cursor_conflict"


@pytest.mark.parametrize("bad_limit", [0, -1, -99])
def test_non_positive_limit_is_named_error(subject, bad_limit):
    _bridge, handler, _rows = subject
    assert json.loads(handler.messages_read("demo", limit=bad_limit))["error"] == "invalid_limit"


def test_foreign_session_cursor_is_named_error(subject):
    _bridge, handler, _rows = subject
    assert json.loads(handler.messages_read("demo", before_id="999"))["error"] == "cursor_foreign_session"


@pytest.mark.parametrize("cursor", ["777", "102"])
def test_unknown_or_non_visible_in_session_cursor_returns_empty(subject, cursor):
    _bridge, handler, _rows = subject
    obj = json.loads(handler.messages_read("demo", before_id=cursor))
    assert obj["messages"] == []
    assert obj["count"] == 0
    assert obj["total_in_session"] == 5


def test_first_message_boundary_returns_empty_not_error(subject):
    _bridge, handler, _rows = subject
    obj = json.loads(handler.messages_read("demo", before_id="101"))
    assert obj["messages"] == []
    assert obj["total_in_session"] == 5


def test_last_message_after_boundary_returns_empty(subject):
    _bridge, handler, _rows = subject
    assert _ids(handler.messages_read("demo", after_id="130")) == []


@pytest.mark.parametrize("cursor", ["bad", "0", "-3", str(2**63), str(2**64)])
def test_invalid_cursor_is_named_error(subject, cursor):
    _bridge, handler, _rows = subject
    assert json.loads(handler.messages_read("demo", after_id=cursor))["error"] == "invalid_cursor"


def test_cursor_owner_uses_read_only_global_id_lookup_and_releases_db(monkeypatch):
    bridge = _load_bridge()
    seen = []

    class Conn:
        def execute(self, sql, params):
            seen.append((sql, params))
            return SimpleNamespace(fetchone=lambda: ("session-2",))

    class DB:
        def _read_ctx(self):
            class Scope:
                def __enter__(self):
                    return Conn()

                def __exit__(self, *_args):
                    return False
            return Scope()

    db = DB()
    monkeypatch.setattr(bridge, "_get_session_db", lambda: db)
    monkeypatch.setitem(sys.modules, "hermes_state_registry",
                        SimpleNamespace(release_or_close=lambda obj: seen.append(("released", obj))))
    assert bridge._cursor_message_session(999) == ("session-2", None)
    assert seen == [
        ("SELECT session_id FROM messages WHERE id=?", (999,)),
        ("released", db),
    ]


# --- Owner-approved newest-first read (kept from the previous round) ---------


def test_newest_first_is_the_default(subject):
    _bridge, handler, _rows = subject
    assert _ids(handler.messages_read("demo", limit=5)) == ALL_VISIBLE
    assert _ids(handler.messages_read("demo", limit=2)) == ["130", "113"]


def test_oldest_mode_reproduces_the_legacy_chronological_page(subject):
    _bridge, handler, _rows = subject
    assert _ids(handler.messages_read("demo", limit=5, order="oldest")) == list(reversed(ALL_VISIBLE))
    # legacy bytes: the most recent N, oldest-first
    assert _ids(handler.messages_read("demo", limit=2, order="oldest")) == ["113", "130"]


def test_newest_mode_is_the_explicit_default(subject):
    _bridge, handler, _rows = subject
    assert _ids(handler.messages_read("demo", limit=2, order="newest")) == ["130", "113"]


@pytest.mark.parametrize("bad_limit", [201, 999, 10**6])
def test_oversized_limit_is_refused(subject, bad_limit):
    """Chosen limit contract: refuse, never silently clamp (truthful wire answer)."""
    _bridge, handler, _rows = subject
    obj = json.loads(handler.messages_read("demo", limit=bad_limit))
    assert obj["error"].startswith("limit_too_large")
    assert "messages" not in obj


def test_oversized_limit_error_states_the_documented_maximum(subject):
    """The error carries the bound, and that bound matches the tool's schema description."""
    _bridge, handler, _rows = subject
    obj = json.loads(handler.messages_read("demo", limit=_bridge._READ_MAX_LIMIT + 1))
    assert obj["error"] == "limit_too_large"
    assert obj["max"] == _bridge._READ_MAX_LIMIT
    described = int(re.search(r"max (\d+)", handler.messages_read.__doc__).group(1))
    assert obj["max"] == described


@pytest.mark.parametrize("bad_limit", [0, -1, -99])
def test_non_positive_limit_is_refused(subject, bad_limit):
    _bridge, handler, _rows = subject
    assert json.loads(handler.messages_read("demo", limit=bad_limit))["error"].startswith("invalid_limit")


def test_string_limit_still_coerces(subject):
    """A client sending limit="2" (string) keeps working; only oversized values are refused."""
    _bridge, handler, _rows = subject
    assert json.loads(handler.messages_read("demo", limit="2"))["count"] == 2


@pytest.mark.parametrize("cursor", ["abc", "0", "-3", "1.5"])
def test_invalid_cursor_is_refused(subject, cursor):
    _bridge, handler, _rows = subject
    assert json.loads(handler.messages_read("demo", before_id=cursor))["error"].startswith("invalid_cursor")


def test_unknown_order_is_refused(subject):
    _bridge, handler, _rows = subject
    assert json.loads(handler.messages_read("demo", order="sideways"))["error"].startswith("invalid_order")


def test_oldest_boundary_returns_empty_page(subject):
    _bridge, handler, _rows = subject
    obj = json.loads(handler.messages_read("demo", before_id="101"))
    assert obj["messages"] == []
    assert obj["count"] == 0
    assert obj["total_in_session"] == 5
    assert obj["next_cursor"] is None


def test_next_cursor_is_the_oldest_id_of_the_page(subject):
    _bridge, handler, _rows = subject
    page = json.loads(handler.messages_read("demo", limit=2))
    assert page["next_cursor"] == "113"
    assert page["total_in_session"] == 5


def test_cursor_pages_include_only_user_and_assistant(subject):
    _bridge, handler, _rows = subject
    assert "102" not in _ids(handler.messages_read("demo", limit=10))


# --- Restored W2b coverage: paging integrity and ownership --------------------


def test_cursor_paging_walks_all_history_without_duplicates_or_gaps(subject):
    _bridge, handler, _rows = subject
    collected = _walk(handler, lambda cursor: {"limit": 2, **({"before_id": cursor} if cursor else {})})
    assert sorted(collected, key=int) == sorted(ALL_VISIBLE, key=int)
    assert len(collected) == len(set(collected)) == 5


def test_page_boundary_is_stable_when_a_new_message_arrives_mid_walk(subject):
    """An id-anchored cursor must not shift when a newer message lands between pages."""
    _bridge, handler, rows = subject
    page1 = json.loads(handler.messages_read("demo", limit=2))
    assert [m["id"] for m in page1["messages"]] == ["130", "113"]
    rows.append({"id": 200, "role": "assistant", "content": "newer", "timestamp": 7})

    collected = [m["id"] for m in page1["messages"]]
    collected += _walk(handler, lambda cursor: {"limit": 2, "before_id": cursor},
                       cursor=page1["next_cursor"])
    assert sorted(collected, key=int) == sorted(ALL_VISIBLE, key=int)
    assert "200" not in collected  # the new id 200 never leaks into an older page


def test_after_id_paging_walks_forward_without_duplicates_or_gaps(subject):
    _bridge, handler, _rows = subject
    collected = _walk(handler, lambda cursor: {"limit": 2, "after_id": cursor or "101"})
    assert collected == ["104", "109", "113", "130"]
    assert len(collected) == len(set(collected)) == 4


def test_after_id_next_cursor_is_the_newest_id_of_the_page(subject):
    _bridge, handler, _rows = subject
    page = json.loads(handler.messages_read("demo", limit=2, after_id="101"))
    assert page["next_cursor"] == "109"


def test_large_but_valid_id_is_not_coerced_and_walks(subject):
    """A SQLite AUTOINCREMENT id can exceed 1e18; the cursor bound is 2**63-1, not 1e18."""
    _bridge, handler, rows = subject
    big = 9_000_000_000_000_000_000  # > 1e18, < 2**63-1
    rows.append({"id": big, "role": "user", "content": "big", "timestamp": 7})
    page = json.loads(handler.messages_read("demo", limit=1))
    assert page["next_cursor"] == str(big)  # exact id, never clamped
    collected = [m["id"] for m in page["messages"]]
    collected += _walk(handler, lambda cursor: {"limit": 2, "before_id": cursor},
                       cursor=page["next_cursor"])
    assert collected[0] == str(big)
    assert len(collected) == len(set(collected)) == 6


def test_walk_page_cap_fails_fast_on_a_cursor_ignoring_handler(subject):
    """Negative control: a handler that ignores the cursor trips the page cap instead of hanging."""
    _bridge, handler, _rows = subject

    def ignoring_cursor(session_key, **kwargs):
        return json.dumps({"session_key": session_key, "count": 1, "total_in_session": 5,
                           "next_cursor": "130", "messages": [{"id": "130"}]})

    handler.messages_read = ignoring_cursor
    with pytest.raises(AssertionError, match="walk exceeded 50 pages"):
        _walk(handler, lambda cursor: {"limit": 2, "before_id": cursor} if cursor else {"limit": 2})


def test_foreign_session_after_cursor_is_named_error(subject):
    _bridge, handler, _rows = subject
    assert json.loads(handler.messages_read("demo", after_id="999"))["error"] == "cursor_foreign_session"


def test_cursor_lookup_failure_is_named_error(subject, monkeypatch):
    _bridge, handler, _rows = subject
    monkeypatch.setattr(_bridge, "_cursor_message_session",
                        lambda message_id: (None, "Failed to read cursor owner: db exploded"))
    assert json.loads(handler.messages_read("demo", before_id="104"))["error"] == "cursor_lookup_failed"
    assert json.loads(handler.messages_read("demo", after_id="104"))["error"] == "cursor_lookup_failed"


@pytest.mark.parametrize("cursor", ["777", "102"])
def test_unknown_or_non_visible_in_session_after_cursor_returns_empty(subject, cursor):
    _bridge, handler, _rows = subject
    obj = json.loads(handler.messages_read("demo", after_id=cursor))
    assert obj["messages"] == []
    assert obj["count"] == 0
    assert obj["total_in_session"] == 5


def test_conversation_messages_exposes_session_id(monkeypatch):
    """Production helper returns (messages, error, session_id) so cursor ownership is checkable."""
    bridge = _load_bridge()
    monkeypatch.setattr(bridge, "_load_sessions_index", lambda: {"demo": {"session_id": "session-1"}})
    monkeypatch.setattr(bridge, "_load_session_messages", lambda sid: ([{"id": 1}], None))
    messages, error, session_id = bridge._conversation_messages("demo")
    assert messages == [{"id": 1}]
    assert error is None
    assert session_id == "session-1"
