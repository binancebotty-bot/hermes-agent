"""Wake-emitter scope: default-deny so only background task completions wake.

A scope is read from plugin config like lane/paths. When present, a completed
turn is emitted ONLY if it satisfies every configured constraint; a rejected
turn must leave no journal row of any kind and must never invoke a sender.
When scope is absent, today's emit-on-every-completed-turn behaviour is kept.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = Path(os.environ.get("WAKE_PLUGIN_DIR", str(ROOT / "plugins" / "wake-emitter")))

LANE = "gpt6:assistant"
CONV = "https://chatgpt.com/c/6b01a297-292c-83ed-9f47-f49ab937c2f1"
SKIPPED = "SKIPPED_OUT_OF_SCOPE"
_NO_SCOPE = object()
_BG = "bg-task-session"
_CHAT = "coordinator-chat"


def _load():
    sys.path.insert(0, str(PLUGIN_DIR.parent))
    return importlib.import_module("wake-emitter.emitter")


class Ctx:
    def __init__(self, cfg):
        self._cfg = cfg
        self.logger = None

    def get_config(self, key, default=None):
        return self._cfg.get(key, default)


def _worker(tmp_path, emitter, scope=_NO_SCOPE):
    """Build the real plugin worker with a fake sender; capture send attempts."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    journal = tmp_path / "journal.jsonl"
    peer = tmp_path / "peer_lane_map.json"
    peer.write_text(json.dumps({"lanes": {LANE: {"conv": CONV, "generation": 1}}}),
                    encoding="utf-8")
    sender = tmp_path / "sender.py"
    sender.write_text("print('GUARD_COMPLETE key=x')\n", encoding="utf-8")
    cfg = {
        "enabled": True, "lane": LANE, "sender_path": str(sender),
        "journal_path": str(journal), "peer_map_path": str(peer),
    }
    if scope is not _NO_SCOPE:
        cfg["scope"] = scope
    return emitter.build_from_ctx(Ctx(cfg)), journal


def _sends(monkeypatch, emitter):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="GUARD_COMPLETE key=x", stderr="")

    monkeypatch.setattr(emitter.subprocess, "run", fake_run)
    return calls


def _payload(**overrides):
    payload = dict(
        session_id=_CHAT, task_id=None, turn_id="turn-1",
        completed=True, failed=False, interrupted=False,
        turn_exit_reason="text_response(stop)", model="m", platform="subagent",
    )
    payload.update(overrides)
    return payload


def _rows(journal):
    if not journal.exists():
        return []
    return [json.loads(line) for line in
            journal.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_scope_is_read_from_ctx_and_defaults_to_none(tmp_path):
    emitter = _load()
    default_worker, _ = _worker(tmp_path / "none", emitter)
    assert default_worker.scope is None
    scoped = {"session_ids": [_BG], "require_task_id": True}
    worker, _ = _worker(tmp_path / "set", emitter, scoped)
    assert worker.scope == scoped


def test_a_unlisted_session_without_task_id_is_skipped_with_no_journal_or_send(tmp_path, monkeypatch):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter, {"session_ids": [_BG]})
    calls = _sends(monkeypatch, emitter)
    assert worker.on_session_end(**_payload(session_id=_CHAT, task_id=None)) == SKIPPED
    assert _rows(journal) == [], "a skipped turn must leave no event AND no outcome"
    assert calls == [], "a skipped turn must never invoke the sender"


def test_b_listed_session_emits_and_delivers(tmp_path, monkeypatch):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter, {"session_ids": [_BG]})
    calls = _sends(monkeypatch, emitter)
    assert worker.on_session_end(**_payload(session_id=_BG, task_id=None)) == "DELIVERED_UNACKED"
    assert [row["kind"] for row in _rows(journal)] == ["event", "attempt", "outcome"]
    assert len(calls) == 1


def test_c_require_task_id_with_task_id_emits(tmp_path, monkeypatch):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter, {"require_task_id": True})
    calls = _sends(monkeypatch, emitter)
    assert worker.on_session_end(**_payload(session_id=_CHAT, task_id="task-9")) == "DELIVERED_UNACKED"
    assert [row["kind"] for row in _rows(journal)] == ["event", "attempt", "outcome"]
    assert len(calls) == 1


def test_d_require_task_id_without_task_id_is_skipped(tmp_path, monkeypatch):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter, {"require_task_id": True})
    calls = _sends(monkeypatch, emitter)
    assert worker.on_session_end(**_payload(session_id=_BG, task_id=None)) == SKIPPED
    assert _rows(journal) == []
    assert calls == []


@pytest.mark.parametrize("session_id,task_id", [
    (_BG, None),        # session matches, task requirement fails
    (_CHAT, "task-9"),  # task requirement matches, session fails
])
def test_e_both_set_but_only_one_satisfied_is_skipped(tmp_path, monkeypatch, session_id, task_id):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter,
                              {"session_ids": [_BG], "require_task_id": True})
    calls = _sends(monkeypatch, emitter)
    assert worker.on_session_end(**_payload(session_id=session_id, task_id=task_id)) == SKIPPED
    assert _rows(journal) == []
    assert calls == []


def test_f_no_scope_keeps_todays_emit_every_turn_behaviour(tmp_path, monkeypatch):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    calls = _sends(monkeypatch, emitter)
    assert worker.on_session_end(**_payload(session_id=_CHAT, task_id=None)) == "DELIVERED_UNACKED"
    assert [row["kind"] for row in _rows(journal)] == ["event", "attempt", "outcome"]
    assert len(calls) == 1


@pytest.mark.parametrize("scope", [
    "gpt6:assistant",
    {"session_ids": _BG},
    {"session_ids": 7},
    {"require_task_id": "yes"},
    {"session_ids": [_BG], "require_task_id": 1},
    [],
])
def test_g_malformed_scope_is_skipped_and_never_raises(tmp_path, monkeypatch, scope):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter, scope)
    calls = _sends(monkeypatch, emitter)
    assert worker.on_session_end(**_payload(session_id=_BG, task_id="task-9")) == SKIPPED
    assert _rows(journal) == []
    assert calls == []


@pytest.mark.parametrize("scope", [
    {},
    {"session_ids": [], "require_task_id": False},
])
def test_scope_present_without_an_effective_rule_default_denies(tmp_path, monkeypatch, scope):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter, scope)
    calls = _sends(monkeypatch, emitter)
    assert worker.on_session_end(**_payload()) == SKIPPED
    assert _rows(journal) == []
    assert calls == []


def test_skipped_turn_does_not_touch_an_existing_journal(tmp_path, monkeypatch):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter, {"session_ids": [_BG]})
    journal.write_text(json.dumps({"kind": "sentinel", "utc": "pre-existing"}) + "\n",
                       encoding="utf-8")
    before = journal.read_text(encoding="utf-8")
    calls = _sends(monkeypatch, emitter)
    assert worker.on_session_end(**_payload(session_id=_CHAT, task_id=None)) == SKIPPED
    assert journal.read_text(encoding="utf-8") == before, \
        "a skipped turn is invisible: no event, no outcome, no ack reconciliation"
    assert calls == []


def test_scope_excluded_task_id_prefix_is_skipped_with_no_journal_or_send(tmp_path, monkeypatch):
    """(a) A matching exclude prefix is an additional deny: no event, no send."""
    emitter = _load()
    scope = {"require_task_id": True, "task_id_exclude_prefixes": ["cron:AAA"]}
    worker, journal = _worker(tmp_path, emitter, scope)
    calls = _sends(monkeypatch, emitter)
    assert worker.on_session_end(**_payload(task_id="cron:AAA:xyz")) == SKIPPED
    assert _rows(journal) == []
    assert calls == []


def test_scope_excluded_prefix_does_not_block_other_task_ids(tmp_path, monkeypatch):
    """(b) A different task_id still emits and delivers normally."""
    emitter = _load()
    scope = {"require_task_id": True, "task_id_exclude_prefixes": ["cron:AAA"]}
    worker, journal = _worker(tmp_path, emitter, scope)
    calls = _sends(monkeypatch, emitter)
    assert worker.on_session_end(**_payload(task_id="cron:BBB:xyz")) == "DELIVERED_UNACKED"
    assert [row["kind"] for row in _rows(journal)] == ["event", "attempt", "outcome"]
    assert len(calls) == 1


def test_scope_exclude_prefix_matching_is_anchored_at_start(tmp_path, monkeypatch):
    """(c) A prefix appearing mid-task_id is not an exclusion."""
    emitter = _load()
    scope = {"require_task_id": True, "task_id_exclude_prefixes": ["cron:AAA"]}
    worker, journal = _worker(tmp_path, emitter, scope)
    calls = _sends(monkeypatch, emitter)
    assert worker.on_session_end(**_payload(task_id="xxxcron:AAA")) == "DELIVERED_UNACKED"
    assert [row["kind"] for row in _rows(journal)] == ["event", "attempt", "outcome"]
    assert len(calls) == 1


@pytest.mark.parametrize("exclude", [
    "cron:AAA",              # a bare string, not a list
    ["cron:AAA", 7],         # non-string entry
    ["cron:AAA", None],      # non-string entry
])
def test_scope_malformed_exclude_value_is_deny_safe(tmp_path, monkeypatch, exclude):
    """(d) Malformed exclude values deny the turn and never raise."""
    emitter = _load()
    scope = {"require_task_id": True, "task_id_exclude_prefixes": exclude}
    worker, journal = _worker(tmp_path, emitter, scope)
    calls = _sends(monkeypatch, emitter)
    assert worker.on_session_end(**_payload(task_id="cron:BBB:xyz")) == SKIPPED
    assert _rows(journal) == []
    assert calls == []


def test_scope_absent_exclude_key_changes_nothing(tmp_path, monkeypatch):
    """(e) Without the key, a cron task_id still wakes exactly as today."""
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter, {"require_task_id": True})
    calls = _sends(monkeypatch, emitter)
    assert worker.on_session_end(**_payload(task_id="cron:AAA:xyz")) == "DELIVERED_UNACKED"
    assert [row["kind"] for row in _rows(journal)] == ["event", "attempt", "outcome"]
    assert len(calls) == 1
