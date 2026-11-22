#!/usr/bin/env python3
"""Self-contained sandbox rehearsal for the wake activation tool.

Builds a disposable fake HOME (fake config.yaml, fake plugin tree, fake
pane_read/sender), then runs apply -> status -> rollback -> rollback and asserts:
  * config restored byte-identically (SHA-256 equality with the pre-apply bytes)
  * plugin dir removed
  * journal path untouched
  * second rollback is a harmless no-op

Nothing outside the scratch dir is written. Usage:
    python scripts/rehearse_activation.py [scratch_dir]
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "wake_activate.py"
PLUGIN = ROOT / "plugins" / "wake-emitter"
LANE = "gpt6:assistant"
CONV = "https://chatgpt.com/c/6b01a297-292c-83ed-9f47-f49ab937c2f1"

CONFIG = """\
# fake hermes config
plugins:
  disabled:
    - some-plugin
  enabled:
    - lane-guard
  entries:
    lane-guard:
      settings:
        granted_capabilities:
          - tools.override
"""

PANE_READ = (
    'import sys\n'
    'lane = sys.argv[1] if len(sys.argv) > 1 else ""\n'
    'print("MATCHED_BY_CONV 6b01a297 (role %s; tab title lags)" % lane)\n'
    'print(\'{"url":"' + CONV + '","selector_chain":"main-div-group","count":1,"turns":[]}\')\n'
)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git(repo, *args):
    subprocess.run(["git", "-c", "user.name=rehearsal", "-c", "user.email=rehearsal@local", *args],
                   cwd=repo, check=True, capture_output=True, text=True)


def build_home(base):
    home = base / "home"
    repo = home / "oc-runs" / "w1-wake-w1"
    src = repo / "plugins" / "wake-emitter"
    src.mkdir(parents=True)
    for item in PLUGIN.iterdir():
        if item.is_file():
            shutil.copy2(item, src / item.name)
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "rehearsal baseline")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()

    hermes = home / ".hermes"
    (hermes / "plugins").mkdir(parents=True)
    (hermes / "state").mkdir()
    (hermes / "scripts").mkdir()
    (hermes / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (hermes / "state" / "peer_lane_map.json").write_text(
        json.dumps({"lanes": {LANE: {"conv": CONV, "generation": 1}}}), encoding="utf-8")
    (hermes / "scripts" / "pane_read.py").write_text(PANE_READ, encoding="utf-8")
    (hermes / "scripts" / "pane_send_guarded.py").write_text(
        "print('GUARD_COMPLETE key=x')\n", encoding="utf-8")
    (hermes / "state" / "event_outbox.jsonl").write_text('{"sentinel": 1}\n', encoding="utf-8")

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["WAKE_EXPECTED_SHA"] = head
    return home, hermes, head, env


def run(env, *args):
    proc = subprocess.run([sys.executable, str(TOOL), *args], env=env,
                          capture_output=True, text=True, timeout=120)
    print(f"$ python scripts/wake_activate.py {' '.join(args)}")
    print(proc.stdout.rstrip())
    if proc.stderr.strip():
        print("[stderr]", proc.stderr.strip())
    print(f"[exit {proc.returncode}]")
    print()
    return proc


def main():
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="wake-rehearse-"))
    base.mkdir(parents=True, exist_ok=True)
    home, hermes, head, env = build_home(base)
    config = hermes / "config.yaml"
    dst = hermes / "plugins" / "wake-emitter"
    journal = hermes / "state" / "event_outbox.jsonl"

    print("=" * 78)
    print("WAKE ACTIVATION REHEARSAL - fake HOME:", home)
    print("pinned sha:", head)
    print("=" * 78)
    print()
    print("pre-apply config sha256:", sha(config))
    print()

    pre = sha(config)
    journal_before = journal.read_bytes()

    applied = run(env, "--apply")
    assert applied.returncode == 0, "apply failed"
    assert "flag_state=ON" in applied.stdout or '"flag_state": "ON"' in applied.stdout
    assert dst.is_dir(), "plugin not installed"
    print("post-apply config sha256:", sha(config))
    print()

    run(env, "--status")

    # An unrelated writer clobbers the config after apply; rollback must undo it.
    config.write_text("plugins:\n  enabled: []\n", encoding="utf-8")
    print("config clobbered post-apply sha256:", sha(config))
    print()
    rolled = run(env, "--rollback")
    assert rolled.returncode == 0, "rollback failed"
    assert sha(config) == pre, "config not restored byte-for-byte"
    assert not dst.exists(), "plugin dir still present"
    assert journal.read_bytes() == journal_before, "journal was touched"
    print("post-rollback config sha256:", sha(config), "== pre-apply:", sha(config) == pre)
    print()

    again = run(env, "--rollback")
    assert again.returncode == 0, "second rollback failed"
    assert sha(config) == pre, "second rollback changed the config"
    assert not dst.exists(), "second rollback resurrected the plugin dir"
    print("second rollback config sha256:", sha(config), "== pre-apply:", sha(config) == pre)
    print()

    print("REHEARSAL PASS: config restored byte-identically, plugin dir removed, "
          "journal untouched, second rollback a no-op.")


if __name__ == "__main__":
    main()
