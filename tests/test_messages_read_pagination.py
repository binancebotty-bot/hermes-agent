"""Contract tests for the mcp-serve bridge's cursor paging.

Run from C:/Users/wigmore/.hermes/plugins/mcp-serve with
    python -m pytest -q tests/test_messages_read_pagination.py

RED first on the unchanged plugin; apply mcp_messages_read_pagination.patch
only after recording the exact failed test output and exit code.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_bridge():
    path = Path(__file__).resolve().parents[1] / "bridge.py"
    spec = importlib.util.spec_from_file_location("mcp_serve_cursor_bridge_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses need a registered module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def subject(monkeypatch):
    bridge = _load_bridge()
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
    ownership = {102: "session-1", 777: None, 999: "session-2"}
    # raising=False lets the first cursor test demonstrate RED against the old implementation.
    monkeypatch.setattr(bridge, "_cursor_message_session",
                        lambda message_id: (ownership.get(message_id), None), raising=False)
    return bridge, bridge._ToolHandlers(bridge=None), rows


def _ids(response):
    return [m["id"] for m in json.loads(response)["messages"]]


def test_red_before_id_is_supported_and_returns_immediately_older(subject):
    _bridge, handler, _rows = subject
    assert _ids(handler.messages_read("demo", limit=2, before_id="113")) == ["104", "109"]


def test_default_no_cursor_exact_bytes_unchanged(subject):
    _bridge, handler, rows = subject
    expected_messages = [
        {"id": str(m["id"]), "role": m["role"], "content": m["content"],
         "timestamp": m["timestamp"]}
        for m in rows if m["role"] in {"user", "assistant"}
    ]
    expected = json.dumps({
        "session_key": "demo", "count": 2, "total_in_session": 5,
        "messages": expected_messages[-2:],
    }, indent=2)
    assert handler.messages_read("demo", limit=2) == expected
    assert _ids(handler.messages_read("demo")) == ["101", "104", "109", "113", "130"]


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


def test_positive_limit_clamps_to_existing_max_200(subject, monkeypatch):
    bridge, handler, _rows = subject
    rows = [{"id": i, "role": "user", "content": str(i), "timestamp": i}
            for i in range(1, 206)]
    monkeypatch.setattr(bridge, "_conversation_messages", lambda key: (rows, None))
    obj = json.loads(handler.messages_read("demo", limit=999))
    assert obj["count"] == 200
    assert obj["total_in_session"] == 205
    assert [obj["messages"][0]["id"], obj["messages"][-1]["id"]] == ["6", "205"]


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


@pytest.mark.parametrize("cursor", ["bad", "0", "-3", str(2**64)])
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
