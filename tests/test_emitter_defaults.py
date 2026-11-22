"""B2: plugin defaults must fail closed and be obviously wrong-on-purpose.

An unconfigured install (no lane, no sender) must end BLOCKED_UNRESOLVED_ORIGIN
and must never invoke a sender. The defaults are read from the real
``build_from_ctx`` in the plugin copy, not re-implemented here.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "wake-emitter"
if __import__("os").environ.get("WAKE_PLUGIN_DIR"):
    PLUGIN_DIR = Path(__import__("os").environ["WAKE_PLUGIN_DIR"])

LANE = "gpt6:assistant"
CONV = "https://chatgpt.com/c/6b01a297-292c-83ed-9f47-f49ab937c2f1"


def _load():
    sys.path.insert(0, str(PLUGIN_DIR.parent))
    return importlib.import_module("wake-emitter.emitter")


class Ctx:
    """Minimal stand-in for PluginContext: get_config over a plain mapping."""

    def __init__(self, cfg):
        self._cfg = cfg
        self.logger = None

    def get_config(self, key, default=None):
        return self._cfg.get(key, default)


def _patch_home(monkeypatch, tmp_path):
    (tmp_path / ".hermes" / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".hermes" / "scripts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls, _p=tmp_path: _p))
    monkeypatch.chdir(tmp_path)


def test_defaults_are_absolute_and_lane_is_unset(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    emitter = _load()
    worker = emitter.build_from_ctx(Ctx({"enabled": True}))
    assert worker.lane is None, "default lane must be unset so resolve_origin refuses"
    assert Path(worker.sender_path).is_absolute(), worker.sender_path
    assert Path(worker.journal_path).is_absolute(), worker.journal_path
    assert Path(worker.peer_map_path).is_absolute(), worker.peer_map_path
    assert Path(worker.pane_read_path).is_absolute(), worker.pane_read_path


def test_unconfigured_install_blocks_and_never_sends(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    emitter = _load()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="DELIVERED to lane", stderr="")

    monkeypatch.setattr(emitter.subprocess, "run", fake_run)
    worker = emitter.build_from_ctx(Ctx({"enabled": True}))
    outcome = worker.on_session_end(
        session_id="sess-1", task_id="task-1", turn_id="turn-1",
        completed=True, failed=False, interrupted=False,
        turn_exit_reason="text_response(stop)", model="m", platform="subagent",
    )
    assert outcome == "BLOCKED_UNRESOLVED_ORIGIN", outcome
    assert calls == [], "an unconfigured install must not invoke a sender"

    rows = [json.loads(line) for line in
            worker.journal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(r.get("outcome") == "BLOCKED_UNRESOLVED_ORIGIN" for r in rows)
    assert not any(r.get("kind") == "attempt" for r in rows), "no write-ahead send intent"


def test_resolve_origin_refuses_none_lane():
    emitter = _load()
    assert emitter.resolve_origin({"lanes": {LANE: {"conv": CONV, "generation": 1}}}, None) is None


def test_configured_install_still_resolves(tmp_path):
    emitter = _load()
    peer = tmp_path / "peer_lane_map.json"
    peer.write_text(json.dumps({"lanes": {LANE: {"conv": CONV, "generation": 1}}}),
                    encoding="utf-8")
    sender = tmp_path / "sender.py"
    sender.write_text("print('GUARD_COMPLETE key=x')\n", encoding="utf-8")
    worker = emitter.build_from_ctx(Ctx({
        "enabled": True, "lane": LANE, "sender_path": str(sender),
        "journal_path": str(tmp_path / "journal.jsonl"),
        "peer_lane_map_path": str(peer),
    }))
    assert worker.lane == LANE
    assert Path(worker.sender_path).is_absolute()
    assert worker._resolve_origin(LANE) == {"conv": CONV, "generation": 1}
