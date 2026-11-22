"""W1b: durable task->conversation+generation binding, fail-closed at send time.

The installed emitter derives the send target solely from one globally configured
lane, so every wake lands in the same conversation. These tests demand a durable
binding store and prove: own-conversation delivery, cross-task isolation,
UNBOUND_TASK fail-closed, stale-generation refusal, restart survival, atomic
concurrent writes and the explicit global-fallback opt-in. They also cover the
two defects found in the same analysis: BUSY write-ahead rows must not strand a
retry as RECONCILE_UNPROVEN, and a literal UNCERTAIN: sender line must not be
treated as a terminal delivery proof.
"""
from __future__ import annotations

import concurrent.futures
import importlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = Path(os.environ.get("WAKE_PLUGIN_DIR", str(ROOT / "plugins" / "wake-emitter")))

LANE_A = "gpt6:assistant"
LANE_B = "gpt7:assistant"
CONV_A = "https://chatgpt.com/c/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CONV_B = "https://chatgpt.com/c/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
GEN = 1


def _load():
    sys.path.insert(0, str(PLUGIN_DIR.parent))
    return importlib.import_module("wake-emitter.emitter")


def _worker(tmp_path, emitter_module, *, allow_fallback=False, with_store=True):
    """Two lanes in the peer map; global lane is LANE_A (conv A)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    journal = tmp_path / "journal.jsonl"
    peer = tmp_path / "peer_lane_map.json"
    peer.write_text(json.dumps({"lanes": {
        LANE_A: {"conv": CONV_A, "generation": GEN},
        LANE_B: {"conv": CONV_B, "generation": GEN},
    }}), encoding="utf-8")
    sender = tmp_path / "sender.py"
    sender.write_text("print('GUARD_COMPLETE key=x')\n", encoding="utf-8")
    kwargs = dict(journal_path=str(journal), peer_map_path=str(peer), sender_path=str(sender),
                  lane=LANE_A, enabled=True)
    if with_store:
        kwargs["binding_path"] = str(tmp_path / "task_bindings.jsonl")
    kwargs["allow_unbound_global_fallback"] = allow_fallback
    return emitter_module.WakeEmitter(**kwargs), journal


def _payload(task_id, *, turn_id=None, session_id=None, completed=True):
    return dict(session_id=session_id or "sess-1", task_id=task_id, turn_id=turn_id,
                completed=completed, failed=False, interrupted=False,
                turn_exit_reason="text_response(stop)", model="m", platform="subagent")


def _rows(journal):
    if not journal.exists():
        return []
    return [json.loads(line) for line in
            journal.read_text(encoding="utf-8").splitlines() if line.strip()]


def _outcomes(journal, event_id=None):
    return [r for r in _rows(journal) if r.get("kind") == "outcome"
            and (event_id is None or r.get("event_id") == event_id)]


def _counting_sender(worker, monkeypatch):
    sends = []
    real_run = worker._run_sender

    def counting(argv):
        sends.append(argv)
        return real_run(argv)

    monkeypatch.setattr(worker, "_run_sender", counting)
    return sends


def _conv_of(argv):
    return argv[argv.index("--expect-conv") + 1]


def _gen_of(argv):
    return argv[argv.index("--expect-gen") + 1]


def _bind(worker, task_id, conv=CONV_B, generation=GEN, *, session_id=None, bound_by="test"):
    return worker.bindings.bind(task_id=task_id, conversation_id=conv, generation=generation,
                                session_id=session_id, bound_by=bound_by)


# --- core binding requirements --------------------------------------------

def test_bound_task_delivers_to_its_own_conversation(tmp_path, monkeypatch):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    sends = _counting_sender(worker, monkeypatch)
    _bind(worker, "task-own")

    result = worker.on_session_end(**_payload("task-own", turn_id="turn-own"))

    assert result == "DELIVERED_UNACKED", result
    assert len(sends) == 1, "exactly one physical send"
    assert sends[0][2] == LANE_B, "sender must aim at the bound conversation's lane"
    assert _conv_of(sends[0]) == CONV_B, "guard must expect the bound conversation"
    assert _gen_of(sends[0]) == str(GEN)


def test_two_tasks_with_different_bindings_do_not_cross(tmp_path, monkeypatch):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    sends = _counting_sender(worker, monkeypatch)
    _bind(worker, "task-A", conv=CONV_B)   # A -> lane B
    _bind(worker, "task-B", conv=CONV_A)   # B -> lane A (the global lane)

    assert worker.on_session_end(**_payload("task-A", turn_id="turn-A")) == "DELIVERED_UNACKED"
    assert worker.on_session_end(**_payload("task-B", turn_id="turn-B")) == "DELIVERED_UNACKED"
    assert len(sends) == 2
    by_conv = {_conv_of(argv): argv for argv in sends}
    assert set(by_conv) == {CONV_A, CONV_B}, "each task must reach its own conversation"
    assert by_conv[CONV_B][2] == LANE_B and by_conv[CONV_A][2] == LANE_A
    # The global-lane task B must not borrow A's binding and vice versa.
    assert _conv_of(sends[0]) == CONV_B and _conv_of(sends[1]) == CONV_A


def test_unbound_task_records_unbound_task_and_sends_nothing(tmp_path, monkeypatch):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)  # store active, global lane resolvable
    sends = _counting_sender(worker, monkeypatch)

    result = worker.on_session_end(**_payload("task-unbound", turn_id="turn-unbound"))

    assert result == "UNBOUND_TASK", result
    assert sends == [], "an unbound task must never fall back to the global lane"
    assert any(r["outcome"] == "UNBOUND_TASK" for r in _outcomes(journal))
    assert not any(r.get("kind") == "attempt" for r in _rows(journal)), "no write-ahead intent"


def test_generation_mismatch_refuses_and_sends_nothing(tmp_path, monkeypatch):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    sends = _counting_sender(worker, monkeypatch)
    _bind(worker, "task-rotated", conv=CONV_B, generation=0)  # peer map is at GEN=1

    result = worker.on_session_end(**_payload("task-rotated", turn_id="turn-rotated"))

    assert result == "STALE_GENERATION", result
    assert sends == [], "never send into a rotated pane"
    row = _outcomes(journal)[-1]
    assert row["outcome"] == "STALE_GENERATION"
    assert row.get("bound_generation") == 0 and row.get("observed_generation") == GEN


def test_binding_survives_fresh_emitter_instance_restart(tmp_path, monkeypatch):
    emitter = _load()
    first, _journal = _worker(tmp_path, emitter)
    _bind(first, "task-restart", conv=CONV_B)

    # A brand-new instance over the same paths: no in-memory state carried over.
    second = emitter.WakeEmitter(
        journal_path=str(first.journal_path), peer_map_path=str(first.peer_map_path),
        sender_path=str(first.sender_path), lane=LANE_A, enabled=True,
        binding_path=str(first.bindings.path), allow_unbound_global_fallback=False)
    sends = _counting_sender(second, monkeypatch)

    result = second.on_session_end(**_payload("task-restart", turn_id="turn-restart"))

    assert result == "DELIVERED_UNACKED", result
    assert len(sends) == 1 and _conv_of(sends[0]) == CONV_B


def test_binding_write_is_atomic_under_concurrent_writers(tmp_path):
    emitter = _load()
    worker, _journal = _worker(tmp_path, emitter)
    store = worker.bindings
    ids = [f"concurrent-{i}" for i in range(12)]

    def write(task_id):
        store.bind(task_id=task_id, conversation_id=CONV_B, generation=GEN,
                   bound_by="thread")

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(write, ids))

    for task_id in ids:
        row = store.lookup(task_id)
        assert row is not None and row["task_id"] == task_id and row["conversation_id"] == CONV_B
    lines = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    assert len(lines) == len(ids), "no concurrent write may be lost or torn"
    assert all(line.get("kind") == "binding" for line in lines)


def test_global_fallback_only_when_explicitly_enabled(tmp_path, monkeypatch):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter, allow_fallback=True)
    sends = _counting_sender(worker, monkeypatch)

    result = worker.on_session_end(**_payload("task-fallback", turn_id="turn-fallback"))

    assert result == "DELIVERED_UNACKED", result
    assert len(sends) == 1 and _conv_of(sends[0]) == CONV_A, "opt-in uses the global lane"


# --- defect (a): BUSY write-ahead row must not strand the retry -----------

def test_busy_pane_retries_on_next_invocation_instead_of_reconcile_unproven(tmp_path, monkeypatch):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    _bind(worker, "task-busy", conv=CONV_B)
    sends = []
    answers = iter(["DELIVERED_BUSY", "DELIVERED_UNACKED"])

    def staged_run(argv):
        sends.append(argv)
        return next(answers)

    monkeypatch.setattr(worker, "_run_sender", staged_run)
    # A readable but empty pane: the old path settles a stranded attempt as
    # RECONCILE_UNPROVEN instead of retrying the no-send BUSY wake on idle.
    monkeypatch.setattr(worker, "_read_pane_turns", lambda lane, timeout: [])

    first = worker.on_session_end(**_payload("task-busy", turn_id="turn-busy"))
    assert first == "DELIVERED_BUSY", first

    second = worker.on_session_end(**_payload("task-busy", turn_id="turn-busy"))
    assert second == "DELIVERED_UNACKED", "BUSY is retryable once the pane is idle"
    assert len(sends) == 2, "the second (idle) invocation must perform a real send"
    assert not any(r["outcome"] in ("RECONCILE_UNPROVEN", "RECONCILE_UNAVAILABLE")
                   for r in _outcomes(journal)), "BUSY must not strand the retry"


# --- defect (b): UNCERTAIN: line is not a delivery proof -------------------

def test_uncertain_sender_line_is_not_terminal_and_reconciles_as_pending(tmp_path, monkeypatch):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    _bind(worker, "task-uncertain", conv=CONV_B)
    sends = []

    # Route through the REAL classifier: the sender emits a literal UNCERTAIN:
    # line with a non-zero exit. It must normalise to DELIVERY_PENDING, never a
    # delivered status, and stay open for the single DELIVERY_PENDING sweep.
    def uncertain_run(argv):
        sends.append(argv)
        return emitter._classify_sender_output("UNCERTAIN:maybe landed\n", 1)

    state = {"event_id": None}
    monkeypatch.setattr(worker, "_run_sender", uncertain_run)
    monkeypatch.setattr(worker, "_read_pane_turns",
                        lambda lane, timeout: (["EVENT-ID: " + state["event_id"]]
                                               if state["event_id"] else []))

    first = worker.on_session_end(**_payload("task-uncertain", turn_id="turn-uncertain"))
    assert first == "DELIVERY_PENDING", "UNCERTAIN must normalise to DELIVERY_PENDING"
    assert first not in ("DELIVERED_UNACKED", "DELIVERED_UNCONFIRMED", "DELIVERED_ACKED"), \
        "an UNCERTAIN line is not proof of delivery"
    event_row = [r for r in _rows(journal) if r.get("kind") == "event"][-1]
    state["event_id"] = event_row["event_id"]

    # A non-emitting later invocation runs only the reconciliation sweep.
    result = worker.on_session_end(**_payload("task-uncertain", turn_id=None, completed=False))
    assert result == "SKIPPED", result
    assert _outcomes(journal)[-1]["outcome"] == "DELIVERED_UNCONFIRMED", \
        "only pane evidence may settle the pending send"
    assert len(sends) == 1, "reconciliation must never blind-resend"
