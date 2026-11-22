"""W1q GREEN proof: fresh Hermes child process, real loader + real hook.

STAGING ONLY. Temp HERMES_HOME, stub transport, no cron, no install, no live
config, no sends, no agent contact. Prints the child's JSON report verbatim.

Proves: discovery -> registration -> hook -> event row -> delivery attempt ->
settlement. Does NOT prove real delivery to GPT or a peer acknowledgement;
the sender and pane readback are stubs.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import test_binding  # noqa: F401  (ensures tests dir importable for the test module)
import test_w1f_non_cron_wake as t
from test_binding import CONV_B, GEN

home = Path(tempfile.mkdtemp(prefix="w1q-fresh-", dir=os.environ.get("TMPDIR"))) / "home"
journal = t._write_throwaway_home(
    home, t._load_plugin_dir(),
    dict(t.CRON_SCOPE, interactive_sessions=[t.INTERACTIVE_SESSION]),
)

env = dict(os.environ)
env.update(HERMES_HOME=str(home), PYTHONPATH=str(t._AGENT_ROOT),
           HERMES_DISABLE_LAZY_INSTALLS="1", PYTHONIOENCODING="utf-8")
env.pop("PYTHONPATH_ORIG", None)

# The reconciliation sweep needs a task-id SHAPE that passes the gate. Use a
# subagent-shaped id so no cron token appears anywhere in this proof.
child_src = t._CHILD.replace("cron:sweep:", "subagent-sweep:")

proc = subprocess.run(
    [sys.executable, "-c", child_src, t.INTERACTIVE_SESSION, str(journal),
     str(home / "state" / "task_bindings.jsonl"), CONV_B, str(GEN)],
    capture_output=True, text=True, timeout=300, env=env, cwd=str(home))

print("CHILD_RC=", proc.returncode)
print(proc.stdout.strip())
if proc.stderr.strip():
    print("STDERR_TAIL=", proc.stderr.strip()[-1500:])

report = json.loads(proc.stdout.strip().splitlines()[-1])
assert proc.returncode == 0, proc.stderr
assert report["discovered"] is True, report
assert report["saw_plugin"] is True, report
kinds = [k for k, _task, _outcome in report["rows"]]
assert kinds.count("event") == 1, report
assert report["rows"][0][1], report          # event carries the uuid minted task_id
assert "attempt" in kinds, report
outcomes = [o for _k, _t, o in report["rows"] if o]
assert outcomes[-1] == "ACKED_ORDER_ONLY", report
print("W1Q_FRESH_GREEN=PASS")
print("UNVERIFIED=real_delivery_to_GPT, peer_acknowledgement (stub sender + stub pane read)")
