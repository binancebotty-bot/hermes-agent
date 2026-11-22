"""The on_session_end hook is documented "never raises"; prove it under fault.

Each test injects one real fault at the exact seam the release review named:
(a) a journal append fault, (b) a malformed peer map (wrong shape, not merely
non-JSON), (c) a BaseException inside the liveness status write. (d) pins the
normal completed-turn journal shape so the fix cannot change delivery semantics.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = Path(os.environ.get("WAKE_PLUGIN_DIR", str(ROOT / "plugins" / "wake-emitter")))

LANE = "gpt6:assistant"
CONV = "https://chatgpt.com/c/6b01a297-292c-83ed-9f47-f49ab937c2f1"


def _load():
    sys.path.insert(0, str(PLUGIN_DIR.parent))
    return importlib.import_module("wake-emitter.emitter")


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)


def _worker(tmp_path, emitter, *, peer_map=None, logger=None):
    journal = tmp_path / "journal.jsonl"
    peer = tmp_path / "peer_lane_map.json"
    peer.write_text(json.dumps({"lanes": {LANE: {"conv": CONV, "generation": 1}}}
                               if peer_map is None else peer_map), encoding="utf-8")
    sender = tmp_path / "sender.py"
    sender.write_text("print('GUARD_COMPLETE key=x')\n", encoding="utf-8")
    return emitter.WakeEmitter(
        journal_path=str(journal), peer_map_path=str(peer), sender_path=str(sender),
        lane=LANE, enabled=True, logger=logger,
    ), journal


def _payload(**overrides):
    payload = dict(
        session_id="sess-1", task_id="task-1", turn_id="turn-1",
        completed=True, failed=False, interrupted=False,
        turn_exit_reason="text_response(stop)", model="m", platform="subagent",
    )
    payload.update(overrides)
    return payload


def _rows(journal):
    return [json.loads(line) for line in
            journal.read_text(encoding="utf-8").splitlines() if line.strip()]


def _outcomes(journal):
    return [row["outcome"] for row in _rows(journal) if row.get("kind") == "outcome"]


def test_journal_append_fault_returns_internal_error(tmp_path, monkeypatch):
    emitter = _load()
    logger = _Logger()
    worker, _journal = _worker(tmp_path, emitter, logger=logger)

    def boom(row):
        raise FileExistsError("journal append failed")

    monkeypatch.setattr(worker, "_append", boom)
    assert worker.on_session_end(**_payload()) == "INTERNAL_ERROR:FileExistsError"
    assert any("FileExistsError" in message for message in logger.messages), \
        "an unwritable journal must fall back to the logger"


def test_malformed_peer_map_returns_internal_error(tmp_path, monkeypatch):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter, peer_map=[])
    assert worker.on_session_end(**_payload()) == "INTERNAL_ERROR:AttributeError"
    rows = _rows(journal)
    events = [row for row in rows if row.get("kind") == "event"]
    outcomes = [row for row in rows if row.get("kind") == "outcome"]
    assert len(events) == 1
    assert [row["outcome"] for row in outcomes] == ["INTERNAL_ERROR:AttributeError"]
    assert outcomes[0]["event_id"] == events[0]["event_id"], \
        "the internal-error outcome must attach to the turn's event"


def test_status_write_base_exception_does_not_mask_primary_outcome(tmp_path, monkeypatch):
    emitter = _load()
    worker, _journal = _worker(tmp_path, emitter)

    def boom(path, record):
        raise KeyboardInterrupt("status write failed")

    monkeypatch.setattr(worker, "_atomic_write_json", boom)
    assert worker.on_session_end(**_payload()) == "DELIVERED_UNACKED"


def test_normal_completed_turn_is_unchanged(tmp_path, monkeypatch):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    assert worker.on_session_end(**_payload()) == "DELIVERED_UNACKED"
    rows = _rows(journal)
    assert [row["kind"] for row in rows] == ["event", "attempt", "outcome"]
    assert [row["outcome"] for row in rows[1:]] == ["ATTEMPTED", "DELIVERED_UNACKED"]
    assert rows[0]["event_id"] == rows[1]["event_id"] == rows[2]["event_id"]
    assert worker.status_path.exists()


def test_internal_fault_outcome_is_honest_not_delivery_or_refusal(tmp_path, monkeypatch):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter, peer_map=[])
    assert worker.on_session_end(**_payload()) == "INTERNAL_ERROR:AttributeError"
    outcomes = _outcomes(journal)
    assert outcomes == ["INTERNAL_ERROR:AttributeError"]
    # DELIVERED_* is exactly the set that suppresses a later retry of the turn.
    assert not any(o.startswith("DELIVERED_") for o in outcomes), \
        "an internal fault must never be recorded as a delivery"
    # REFUSED:* is a no-send the retry logic treats as progress; an internal
    # fault is neither a delivery nor a no-send.
    assert not any(o.startswith("REFUSED:") for o in outcomes), \
        "an internal fault must not be recorded as a no-send refusal"


def test_internal_fault_returns_string_and_journal_has_no_delivery_row(tmp_path, monkeypatch):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter, peer_map=[])
    result = worker.on_session_end(**_payload())
    assert isinstance(result, str), "the hook must return a status string, not raise"
    assert result == "INTERNAL_ERROR:AttributeError"
    rows = _rows(journal)
    assert any(row.get("kind") == "outcome"
               and row.get("outcome") == "INTERNAL_ERROR:AttributeError" for row in rows)
    assert not any(row.get("kind") == "outcome"
                   and str(row.get("outcome", "")).startswith("DELIVERED_") for row in rows)
