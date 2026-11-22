"""One configured python_executable must govern BOTH emitter child processes.

Production failure this pins: after a hot plugin reload, a real cron completion
fired the hook, but sys.executable was the Hermes tools Python, which has no
`websocket` module, so pane_send_guarded.py died (REFUSED:EXIT_1). The same
applied to the ACK pane read. The fix reads one optional `python_executable`
config key and uses it at both child invocations, falling back to sys.executable.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = Path(os.environ.get("WAKE_PLUGIN_DIR", str(ROOT / "plugins" / "wake-emitter")))

LANE = "gpt6:assistant"
CONV = "https://chatgpt.com/c/6b01a297-292c-83ed-9f47-f49ab937c2f1"
CFG_PY = "C:/configured/python.exe"


def _load():
    sys.path.insert(0, str(PLUGIN_DIR.parent))
    return importlib.import_module("wake-emitter.emitter")


class Ctx:
    """Stand-in for PluginContext: get_config over a plain mapping."""

    def __init__(self, cfg):
        self._cfg = cfg
        self.logger = None

    def get_config(self, key, default=None):
        return self._cfg.get(key, default)


def _payload(**overrides):
    payload = dict(
        session_id="sess-1", task_id="task-1", turn_id="turn-1",
        completed=True, failed=False, interrupted=False,
        turn_exit_reason="text_response(stop)", model="m", platform="subagent",
    )
    payload.update(overrides)
    return payload


def _fixture(tmp_path, monkeypatch, emitter, *, python_executable=None, run=None, calls=None):
    """Build a worker via the real build_from_ctx and record every child argv."""
    if calls is None:
        calls = []
    (tmp_path / ".hermes" / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".hermes" / "scripts").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls, _p=tmp_path: _p))
    monkeypatch.chdir(tmp_path)

    peer = tmp_path / "peer_lane_map.json"
    peer.write_text(json.dumps({"lanes": {LANE: {"conv": CONV, "generation": 1}}}),
                    encoding="utf-8")
    sender = tmp_path / "sender.py"
    sender.write_text("print('GUARD_COMPLETE key=x')\n", encoding="utf-8")
    pane_read = tmp_path / "pane_read.py"
    pane_read.write_text("", encoding="utf-8")

    cfg = {
        "enabled": True,
        "lane": LANE,
        "sender_path": str(sender),
        "pane_read_path": str(pane_read),
        "journal_path": str(tmp_path / "journal.jsonl"),
        "peer_map_path": str(peer),
    }
    if python_executable is not None:
        cfg["python_executable"] = python_executable

    def default_run(argv, **kwargs):
        calls.append(list(argv))
        script = argv[1] if len(argv) > 1 else ""
        if script == str(sender):
            return SimpleNamespace(returncode=0, stdout="GUARD_COMPLETE key=x", stderr="")
        if script == str(pane_read):
            return SimpleNamespace(returncode=0, stdout=json.dumps({"turns": []}), stderr="")
        raise AssertionError("unexpected child: %r" % (argv,))

    monkeypatch.setattr(emitter.subprocess, "run", run or default_run)
    worker = emitter.build_from_ctx(Ctx(cfg))
    return worker, calls, str(sender), str(pane_read)


def _rows(worker):
    return [json.loads(line) for line in
            worker.journal_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_send_invocation_uses_configured_python_executable(tmp_path, monkeypatch):
    """(a) SEND child argv[0] is exactly the configured value."""
    emitter = _load()
    worker, calls, sender, _pane = _fixture(tmp_path, monkeypatch, emitter,
                                            python_executable=CFG_PY)
    outcome = worker.on_session_end(**_payload())
    assert outcome == "DELIVERED_UNACKED", outcome
    sends = [c for c in calls if c[1] == sender]
    assert sends, "sender was never invoked"
    assert sends[0][0] == CFG_PY, sends[0]


def test_pane_read_invocation_uses_configured_python_executable(tmp_path, monkeypatch):
    """(b) PANE READ child argv[0] is the same configured value."""
    emitter = _load()
    worker, calls, _sender, pane = _fixture(tmp_path, monkeypatch, emitter,
                                            python_executable=CFG_PY)
    assert worker._read_pane_turns(LANE, 5) == []
    reads = [c for c in calls if c[1] == pane]
    assert reads, "pane read was never invoked"
    assert reads[0][0] == CFG_PY, reads[0]


def test_both_children_fall_back_to_sys_executable_when_unset(tmp_path, monkeypatch):
    """(c) with the key unset, BOTH children use sys.executable."""
    emitter = _load()
    worker, calls, sender, pane = _fixture(tmp_path, monkeypatch, emitter)
    assert worker.python_executable == sys.executable
    worker.on_session_end(**_payload())
    worker._read_pane_turns(LANE, 5)
    sends = [c for c in calls if c[1] == sender]
    reads = [c for c in calls if c[1] == pane]
    assert sends and reads, calls
    assert sends[0][0] == sys.executable, sends[0]
    assert reads[0][0] == sys.executable, reads[0]


def test_bogus_python_executable_yields_truthful_refusal_without_raising(tmp_path, monkeypatch):
    """(d) a bogus interpreter produces REFUSED, not an escaping exception."""
    emitter = _load()
    bogus = "C:/definitely/missing/python.exe"

    def crash_for_bogus(argv, **kwargs):
        # A real interpreter would launch the child; the bogus one dies before
        # printing any delivery proof -- the production REFUSED:EXIT_1 shape.
        if argv[0] == bogus:
            return SimpleNamespace(
                returncode=1, stdout="",
                stderr="ModuleNotFoundError: No module named 'websocket'")
        return SimpleNamespace(returncode=0, stdout="GUARD_COMPLETE key=x", stderr="")

    calls = []

    def recording_run(argv, **kwargs):
        calls.append(list(argv))
        return crash_for_bogus(argv, **kwargs)

    worker, calls, sender, _pane = _fixture(tmp_path, monkeypatch, emitter,
                                            python_executable=bogus, run=recording_run,
                                            calls=calls)
    outcome = worker.on_session_end(**_payload())  # must not raise
    assert isinstance(outcome, str), outcome
    assert outcome.startswith("REFUSED:"), outcome
    sends = [c for c in calls if c[1] == sender]
    assert sends and sends[0][0] == bogus, sends
    rows = _rows(worker)
    assert any(r.get("kind") == "outcome"
               and str(r.get("outcome", "")).startswith("REFUSED:") for r in rows), rows
    assert not any(str(r.get("outcome", "")).startswith("DELIVERED") for r in rows), rows
