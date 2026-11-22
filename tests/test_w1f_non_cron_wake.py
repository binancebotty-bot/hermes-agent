"""W1f: a genuine NON-CRON completion must produce a wake event.

Measured defect this file pins: every row in the live journal is cron-sourced.
Two independent causes, both proved here:

(a) LOAD-TIME (runtime, outside this repo): ``PluginManager.discover_and_load``
    scans once per process (hermes_cli/plugins.py:393-409) and
    ``hermes_cli.plugins._delivery_manager`` never re-scans (plugins.py:990-999).
    A long-lived chat/gateway process that booted before the plugin was installed
    therefore has no ``on_session_end`` subscriber, so
    ``agent/turn_finalizer.py:911`` fires into an empty registry. A fresh process
    does register it — ``test_hook_dispatch_reaches_plugin_in_a_fresh_process``
    proves that through the real dispatch chain, with no cron anywhere.

(b) SCOPE (this repo): ``_in_scope`` admits only ``task_id_prefix_allow``
    prefixes. A genuine interactive turn carries the task id the runtime minted
    for it (``agent/turn_context.py:643``: ``task_id or str(uuid.uuid4())``), and
    no prefix list can match a uuid, so the turn is SKIPPED_OUT_OF_SCOPE before a
    single journal row is written (emitter.py:708-710). ``require_task_id: false``
    does not help: the id is present, it is just unrecognisable.

The fix under test is one explicit, session-scoped admission key:
``scope.interactive_sessions`` — a listed session's completed turns are in scope
whatever shape their minted task id has. Absent, empty or malformed, nothing
widens: the deny default stands.

Every fixture is a stub transport / throwaway home. Nothing installed, sent,
configured, restarted or contacted.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from test_binding import CONV_B, GEN, _bind, _load, _outcomes, _payload, _rows, _worker

INTERACTIVE_SESSION = "local-interactive-session-w1f"
OTHER_SESSION = "local-some-other-chat"
CRON_SCOPE = {"require_task_id": True, "task_id_prefix_allow": ["cron:", "subagent-"]}
SETTLED = ("DELIVERED_ACKED", "ACKED_ORDER_ONLY")


def _interactive_payload(*, session_id=INTERACTIVE_SESSION, task_id=None, turn_id=None):
    """Exactly the nine kwargs agent/turn_finalizer.py:911-922 passes."""
    return dict(
        session_id=session_id,
        task_id=task_id if task_id is not None else str(uuid.uuid4()),
        turn_id=turn_id or "turn-" + uuid.uuid4().hex[:8],
        completed=True,
        failed=False,
        interrupted=False,
        turn_exit_reason="text_response(stop)",
        model="m",
        platform="desktop",
    )


def _two_lane_worker(tmp_path, scope=None):
    emitter = _load()
    worker, journal = _worker(tmp_path, emitter)
    worker.scope = scope
    return emitter, worker, journal


def _sends(worker, monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "_run_sender",
                        lambda argv: (calls.append(argv), "DELIVERED_UNACKED")[1])
    return calls


def _settle(worker, journal, monkeypatch, event_id):
    """One reconcile sweep over a delivery anchor plus a WAKE-ACK reply.

    The sweep payload must itself pass the scope gate (a cron-shaped id is always
    admitted) and must not be a completed turn, so the invocation reconciles only.
    """
    turns = [{"text": "EVENT-ID: " + event_id},
             {"text": "WAKE-ACK " + event_id}]
    monkeypatch.setattr(worker, "_read_pane_turns", lambda lane, timeout, window=None: list(turns))
    worker.on_session_end(**_payload("cron:sweep:" + uuid.uuid4().hex, turn_id=None,
                                     completed=False))
    return [row.get("outcome") for row in _outcomes(journal, event_id)]


# --- (a) today's behaviour: the scope gate swallows a real chat turn --------

def test_interactive_turn_is_out_of_scope_under_the_cron_allowlist(tmp_path, monkeypatch):
    """The measured defect: a genuine chat turn is skipped before any row."""
    _emitter, worker, journal = _two_lane_worker(tmp_path, CRON_SCOPE)
    calls = _sends(worker, monkeypatch)

    assert worker.on_session_end(**_interactive_payload()) == "SKIPPED_OUT_OF_SCOPE"
    assert _rows(journal) == [], "a skipped turn must leave no journal row"
    assert calls == [], "a skipped turn must never invoke the transport"


def test_require_task_id_false_alone_does_not_admit_an_interactive_turn(tmp_path, monkeypatch):
    """Q3: relaxing require_task_id is not enough — the uuid still fails the prefix gate."""
    scope = {"require_task_id": False, "session_ids": [INTERACTIVE_SESSION],
             "task_id_prefix_allow": ["cron:", "subagent-"]}
    _emitter, worker, journal = _two_lane_worker(tmp_path, scope)
    calls = _sends(worker, monkeypatch)

    assert worker.on_session_end(**_interactive_payload()) == "SKIPPED_OUT_OF_SCOPE"
    assert _rows(journal) == [] and calls == []


# --- the fix: one listed session admits genuine interactive completions -----

def test_listed_interactive_session_records_delivers_and_settles(tmp_path, monkeypatch):
    """A listed session's real turn produces event -> attempt -> settled ACK."""
    scope = dict(CRON_SCOPE, interactive_sessions=[INTERACTIVE_SESSION])
    _emitter, worker, journal = _two_lane_worker(tmp_path, scope)
    payload = _interactive_payload()
    _bind(worker, payload["task_id"], conv=CONV_B, generation=GEN)
    calls = _sends(worker, monkeypatch)

    assert worker.on_session_end(**payload) == "DELIVERED_UNACKED"
    kinds = [row.get("kind") for row in _rows(journal)]
    assert kinds == ["event", "attempt", "outcome"], kinds
    assert len(calls) == 1, "exactly one physical send for one completion"

    event_id = _rows(journal)[0]["event_id"]
    names = _settle(worker, journal, monkeypatch, event_id)
    assert names[-1] in SETTLED, "an acknowledged wake must settle: %s" % (names,)
    assert len(calls) == 1, "settlement must never resend"


def test_unlisted_interactive_session_stays_denied(tmp_path, monkeypatch):
    """Fail-closed: only the listed session is admitted."""
    scope = dict(CRON_SCOPE, interactive_sessions=[OTHER_SESSION])
    _emitter, worker, journal = _two_lane_worker(tmp_path, scope)
    calls = _sends(worker, monkeypatch)

    assert worker.on_session_end(**_interactive_payload()) == "SKIPPED_OUT_OF_SCOPE"
    assert _rows(journal) == [] and calls == []


@pytest.mark.parametrize("bad", ["local-interactive-session-w1f", [None], [7], [""], {"a": 1}])
def test_malformed_interactive_sessions_denies_and_never_raises(tmp_path, monkeypatch, bad):
    """A malformed consent list is deny-safe, like every other scope rule."""
    scope = dict(CRON_SCOPE, interactive_sessions=bad)
    _emitter, worker, journal = _two_lane_worker(tmp_path, scope)
    calls = _sends(worker, monkeypatch)

    assert worker.on_session_end(**_interactive_payload()) == "SKIPPED_OUT_OF_SCOPE"
    assert _rows(journal) == [] and calls == []


def test_empty_interactive_sessions_is_not_an_effective_rule(tmp_path, monkeypatch):
    """An all-empty scope still allows nothing (the historical default-deny)."""
    _emitter, worker, journal = _two_lane_worker(tmp_path, {"interactive_sessions": []})
    calls = _sends(worker, monkeypatch)

    assert worker.on_session_end(**_interactive_payload()) == "SKIPPED_OUT_OF_SCOPE"
    assert _rows(journal) == [] and calls == []


def test_interactive_admission_cannot_bypass_the_exclude_deny_list(tmp_path, monkeypatch):
    """Admission by session does not lift an explicit task_id exclude."""
    scope = dict(CRON_SCOPE, interactive_sessions=[INTERACTIVE_SESSION],
                 task_id_exclude_prefixes=["local-"])
    _emitter, worker, journal = _two_lane_worker(tmp_path, scope)
    calls = _sends(worker, monkeypatch)

    assert worker.on_session_end(
        **_interactive_payload(task_id="local-excluded-1")) == "SKIPPED_OUT_OF_SCOPE"
    assert _rows(journal) == [] and calls == []


# --- the other genuine non-cron source: a delegated subagent ----------------

def test_subagent_completion_is_already_in_scope_and_settles(tmp_path, monkeypatch):
    """subagent-N-<hex> matches the existing allowlist, so no scope change is needed."""
    _emitter, worker, journal = _two_lane_worker(tmp_path, CRON_SCOPE)
    task_id = "subagent-1-ab12cd34"
    _bind(worker, task_id, conv=CONV_B, generation=GEN)
    calls = _sends(worker, monkeypatch)

    assert worker.on_session_end(**_payload(task_id, turn_id="turn-" + task_id)) == "DELIVERED_UNACKED"
    assert [row.get("kind") for row in _rows(journal)] == ["event", "attempt", "outcome"]
    assert len(calls) == 1

    event_id = _rows(journal)[0]["event_id"]
    names = _settle(worker, journal, monkeypatch, event_id)
    assert names[-1] in SETTLED, names


def test_cron_completion_still_emits_unchanged(tmp_path, monkeypatch):
    """Regression guard: cron stays admitted and unchanged."""
    _emitter, worker, journal = _two_lane_worker(tmp_path, CRON_SCOPE)
    task_id = "cron:job1:" + uuid.uuid4().hex
    _bind(worker, task_id, conv=CONV_B, generation=GEN)
    calls = _sends(worker, monkeypatch)

    assert worker.on_session_end(**_payload(task_id, turn_id="turn-cron")) == "DELIVERED_UNACKED"
    assert len(calls) == 1


# --- (a) load-time proof through the REAL dispatch chain -------------------

_AGENT_ROOT = Path(os.environ.get("HERMES_AGENT_ROOT",
                                  str(Path.home() / ".hermes" / "hermes-agent")))
_CHILD = r'''
import json, sys, uuid
from pathlib import Path
from hermes_cli import lifecycle
from hermes_cli.plugins import get_plugin_manager

session, journal_path, binding_path = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
conv, gen = sys.argv[4], int(sys.argv[5])
binding_path.write_text(json.dumps({
    "kind": "binding", "task_id": None, "session_id": session,
    "conversation_id": conv, "generation": gen, "bound_by": "w1f-test"}) + "\n",
    encoding="utf-8")

payload = dict(
    session_id=session, task_id=str(uuid.uuid4()), turn_id="turn-fresh-1",
    completed=True, failed=False, interrupted=False,
    turn_exit_reason="text_response(stop)", model="m", platform="desktop",
)
first = lifecycle.invoke_hook("on_session_end", **payload)
# A later non-completing turn runs only the ACK sweep over the stub pane read.
sweep = lifecycle.invoke_hook("on_session_end", **dict(
    payload, task_id="cron:sweep:" + uuid.uuid4().hex, turn_id=None, completed=False))
manager = get_plugin_manager()
rows = [json.loads(l) for l in journal_path.read_text(encoding="utf-8").splitlines() if l.strip()] \
    if journal_path.exists() else []
print(json.dumps({
    "hook_results": [str(r) for r in first], "sweep_result": [str(r) for r in sweep],
    "discovered": bool(getattr(manager, "_discovered", False)),
    "saw_plugin": "wake-emitter" in getattr(manager, "_plugins", {}),
    "rows": [(r.get("kind"), r.get("task_id"), r.get("outcome")) for r in rows],
}))
'''


def _write_throwaway_home(home: Path, plugin_src: Path, scope: dict) -> Path:
    import shutil
    import yaml

    (home / "plugins").mkdir(parents=True)
    (home / "state").mkdir(parents=True)
    (home / "scripts").mkdir(parents=True)
    shutil.copytree(plugin_src, home / "plugins" / "wake-emitter")
    (home / "scripts" / "stub_sender.py").write_text(
        "print('GUARD_COMPLETE key=x')\n", encoding="utf-8")
    journal = home / "state" / "event_outbox.jsonl"
    # Stub readback: acknowledges the newest recorded event, so settlement is real.
    # It is invoked as `<python> <script> <lane> --json`, so the journal path is baked in.
    (home / "scripts" / "stub_pane_read.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "rows = [json.loads(l) for l in Path(r'%s').read_text(encoding='utf-8')\n"
        "        .splitlines() if l.strip()]\n"
        "eid = [r['event_id'] for r in rows if r.get('kind') == 'event'][-1]\n"
        "print(json.dumps({'turns': [{'text': 'EVENT-ID: ' + eid},\n"
        "                            {'text': 'WAKE-ACK ' + eid}]}))\n" % journal,
        encoding="utf-8")
    (home / "state" / "peer_lane_map.json").write_text(json.dumps(
        {"lanes": {"gpt6:assistant": {"conv": CONV_B, "generation": GEN}}}), encoding="utf-8")
    settings = {
        "enabled": True, "lane": "gpt6:assistant",
        "journal_path": str(journal),
        # build_from_ctx reads `peer_map_path` (emitter.py:1546). The live config's
        # `peer_lane_map_path` key is not read at all (reported as a W1f finding);
        # the fixture sets the key the code actually honours.
        "peer_map_path": str(home / "state" / "peer_lane_map.json"),
        "sender_path": str(home / "scripts" / "stub_sender.py"),
        "pane_read_path": str(home / "scripts" / "stub_pane_read.py"),
        "binding_path": str(home / "state" / "task_bindings.jsonl"),
        "python_executable": sys.executable,
        "scope": scope,
    }
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {
        "enabled": ["wake-emitter"],
        "entries": {"wake-emitter": {"settings": settings}},
    }}), encoding="utf-8")
    return journal


def test_hook_dispatch_reaches_plugin_in_a_fresh_process(tmp_path):
    """A FRESH Hermes process discovers the plugin and wakes on a chat turn.

    This is the load-time half, end to end through the real dispatch chain:
    lifecycle.invoke_hook -> plugin discovery -> register_hook -> emitter ->
    journal + stub transport + W1e settlement. No cron, no install, no config.

    A long-lived chat or gateway process that started before the plugin existed
    never re-scans (plugins.py:408), so its turns dispatch into an empty
    registry — which is why only cron (a new process per run) ever produced a row.
    """
    if not (_AGENT_ROOT / "hermes_cli" / "lifecycle.py").exists():
        pytest.skip("hermes-agent runtime not present at %s" % _AGENT_ROOT)
    home = tmp_path / "home"
    scope = dict(CRON_SCOPE, interactive_sessions=[INTERACTIVE_SESSION])
    journal = _write_throwaway_home(home, _load_plugin_dir(), scope)

    env = dict(os.environ)
    env.update(HERMES_HOME=str(home), PYTHONPATH=str(_AGENT_ROOT),
               HERMES_DISABLE_LAZY_INSTALLS="1", PYTHONIOENCODING="utf-8")
    env.pop("PYTHONPATH_ORIG", None)
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, INTERACTIVE_SESSION, str(journal),
         str(home / "state" / "task_bindings.jsonl"), CONV_B, str(GEN)],
        capture_output=True, text=True, timeout=300, env=env, cwd=str(home))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])

    assert report["discovered"] is True, report
    assert report["saw_plugin"] is True, "a fresh process must load the plugin: %s" % report
    kinds = [kind for kind, _task, _outcome in report["rows"]]
    assert kinds.count("event") == 1, "one completion, one event: %s" % report
    assert report["rows"][0][1], "the event must carry the interactive turn's task id: %s" % report
    assert "attempt" in kinds, "the hook must attempt delivery: %s" % report
    outcomes = [outcome for _kind, _task, outcome in report["rows"] if outcome]
    assert outcomes[-1] == "ACKED_ORDER_ONLY", (
        "a timestamp-less readback must settle ACKED_ORDER_ONLY: %s" % report)


def _load_plugin_dir() -> Path:
    return Path(os.environ.get("WAKE_PLUGIN_DIR",
                               str(Path(__file__).resolve().parents[1] / "plugins" / "wake-emitter")))
