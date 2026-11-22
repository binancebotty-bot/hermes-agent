"""FIX 1: scope is an allow-list by ``task_id_prefix_allow``.

A minted uuid, an interactive/desktop turn, a gateway session turn and the
cron-induced coordinator turn must all be SKIPPED_OUT_OF_SCOPE with zero journal
rows and no sender call. A malformed allow-list denies safely and never raises.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = Path(os.environ.get("WAKE_PLUGIN_DIR", str(ROOT / "plugins" / "wake-emitter")))

LANE = "gpt6:assistant"
CONV = "https://chatgpt.com/c/6b01a297-292c-83ed-9f47-f49ab937c2f1"
SKIPPED = "SKIPPED_OUT_OF_SCOPE"
ALLOW = ["cron:", "subagent-"]


def _load():
    sys.path.insert(0, str(PLUGIN_DIR.parent))
    return importlib.import_module("wake-emitter.emitter")


def _worker(tmp_path, emitter, *, scope=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    journal = tmp_path / "journal.jsonl"
    peer = tmp_path / "peer_lane_map.json"
    peer.write_text(json.dumps({"lanes": {LANE: {"conv": CONV, "generation": 1}}}),
                    encoding="utf-8")
    sender = tmp_path / "sender.py"
    sender.write_text("print('GUARD_COMPLETE key=x')\n", encoding="utf-8")
    return emitter.WakeEmitter(
        journal_path=str(journal), peer_map_path=str(peer), sender_path=str(sender),
        lane=LANE, enabled=True, scope=scope), journal


def _payload(**overrides):
    payload = dict(session_id="sess-1", task_id="cron:abc:1", turn_id="turn-1",
                   completed=True, failed=False, interrupted=False,
                   turn_exit_reason="text_response(stop)", model="m", platform="cron")
    payload.update(overrides)
    return payload


def _rows(journal):
    if not journal.exists():
        return []
    return [json.loads(line) for line in
            journal.read_text(encoding="utf-8").splitlines() if line.strip()]


def _sends(monkeypatch, emitter):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="GUARD_COMPLETE key=x", stderr="")

    monkeypatch.setattr(emitter.subprocess, "run", fake_run)
    return calls


def test_a_uuid_task_id_is_skipped_under_prefix_allow(tmp_path, monkeypatch):
    """(a) A minted uuid task_id is out of scope: no event, no send."""
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter, scope={"task_id_prefix_allow": ALLOW})
    calls = _sends(monkeypatch, emitter)
    assert worker.on_session_end(**_payload(task_id=str(uuid.uuid4()))) == SKIPPED
    assert _rows(journal) == [], "a skipped turn must leave no journal row"
    assert calls == [], "a skipped turn must never invoke the sender"


def test_b_allowed_prefix_passes_and_delivers(tmp_path, monkeypatch):
    """(b) task_id cron:xyz is in scope and delivers exactly once."""
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter, scope={"task_id_prefix_allow": ALLOW})
    calls = _sends(monkeypatch, emitter)
    assert worker.on_session_end(**_payload(task_id="cron:xyz")) == "DELIVERED_UNACKED"
    assert [row["kind"] for row in _rows(journal)] == ["event", "attempt", "outcome"]
    assert len(calls) == 1


def test_c_induced_coordinator_turn_is_skipped(tmp_path, monkeypatch):
    """(c) The cron-induced coordinator turn (task_id local-...) is out of scope."""
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter, scope={"task_id_prefix_allow": ALLOW})
    calls = _sends(monkeypatch, emitter)
    task_id = "local-50192198eedbb0e0f54aa94821baab9c93bf7e68d3ad9f25cbd1d2ab97682617"
    assert worker.on_session_end(**_payload(task_id=task_id)) == SKIPPED
    assert _rows(journal) == []
    assert calls == []


@pytest.mark.parametrize("allow", [
    "cron:",                    # a bare string, not a list
    ["cron:", 7],               # non-string entry
    ["cron:", None],            # non-string entry
    ["cron:", "subagent-", 3],  # one bad entry among good ones
])
def test_d_malformed_allow_list_denies_and_never_raises(tmp_path, monkeypatch, allow):
    """(d) A malformed allow-list denies the turn and never raises."""
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter, scope={"task_id_prefix_allow": allow})
    calls = _sends(monkeypatch, emitter)
    assert worker.on_session_end(**_payload(task_id="cron:abc:1")) == SKIPPED
    assert _rows(journal) == []
    assert calls == []


def test_empty_allow_list_keeps_todays_behaviour(tmp_path, monkeypatch):
    """An empty allow-list is a no-op: a cron task_id still wakes as today."""
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter, scope={"require_task_id": True,
                                                        "task_id_prefix_allow": []})
    calls = _sends(monkeypatch, emitter)
    assert worker.on_session_end(**_payload(task_id="cron:abc:1")) == "DELIVERED_UNACKED"
    assert len(calls) == 1


def test_allow_list_composes_with_exclude_prefix(tmp_path, monkeypatch):
    """The allow-list is an additional gate on top of the exclude deny-list."""
    emitter = _load()
    scope = {"task_id_prefix_allow": ALLOW, "task_id_exclude_prefixes": ["cron:ABC"]}
    worker, journal = _worker(tmp_path, emitter, scope=scope)
    calls = _sends(monkeypatch, emitter)
    assert worker.on_session_end(**_payload(task_id="cron:ABC:1")) == SKIPPED
    assert worker.on_session_end(**_payload(task_id="subagent-1-x")) == "DELIVERED_UNACKED"
    assert len(calls) == 1
