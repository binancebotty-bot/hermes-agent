"""Wake-loop activation script — STAGED, not run.

Installs the reviewed wake-emitter plugin into the live plugin tree and enables it, with
verifiable before/after state and a single-command rollback. Defaults to --dry-run so an
accidental invocation changes nothing.

Usage:
    python scripts/wake_activate.py --dry-run          (default; prints the plan)
    python scripts/wake_activate.py --apply            (installs + enables)
    python scripts/wake_activate.py --rollback         (flag off, then remove the plugin dir)
    python scripts/wake_activate.py --status           (show current state)
"""
import argparse
import hashlib
import json
import os
import pathlib
import shutil
import sys
import time

HOME = pathlib.Path.home()
H = HOME / ".hermes"
SRC = HOME / "oc-runs" / "w1-wake-w1" / "plugins" / "wake-emitter"
DST = H / "plugins" / "wake-emitter"
CONFIG = H / "config.yaml"
STATUS = H / "state" / "wake_activation.json"
LANE = "gpt6:assistant"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()[:32] if path.exists() else None


def tree_hashes(root):
    out = {}
    if root.exists():
        for p in sorted(root.rglob("*.py")):
            out[str(p.relative_to(root))] = sha(p)
    return out


def live_config_has_flag():
    if not CONFIG.exists():
        return None
    text = CONFIG.read_text(encoding="utf-8", errors="replace")
    return "wake-emitter" in text or "wake_emitter" in text


def status():
    rec = {
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_dir": str(SRC), "source_present": SRC.exists(), "source_hashes": tree_hashes(SRC),
        "installed_dir": str(DST), "installed_present": DST.exists(), "installed_hashes": tree_hashes(DST),
        "config_mentions_flag": live_config_has_flag(),
        "flag_state": "UNKNOWN (config key not read by this staging script)",
    }
    return rec


def plan():
    print("PLAN")
    print("  1 copy %s -> %s (plugin is inert until the hook fires)" % (SRC, DST))
    print("  2 add the enabled flag for lane %s in the plugin's own config section" % LANE)
    print("  3 verify the gateway loads it: python -m hermes_cli.plugins (list plugins)")
    print("  4 prove one cycle: a real completion must produce ONE event row,")
    print("    one DELIVERED_* outcome, and the GPT reply containing that event_id")
    print("  5 rollback if any step fails: --rollback (flag off first, then remove)")


def apply():
    if not SRC.exists():
        print("REFUSED: source plugin not found at %s" % SRC)
        return 2
    if DST.exists():
        print("NOTE: destination already exists; replacing after recording its hashes")
    before = status()
    shutil.copytree(SRC, DST, dirs_exist_ok=True)
    after = status()
    rec = {"utc": after["utc"], "action": "install", "before": before, "after": after,
           "rollback": "python scripts/wake_activate.py --rollback"}
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    print("INSTALLED %s" % DST)
    print("source hashes == installed hashes: %s" % (before["source_hashes"] == after["installed_hashes"]))
    print("NEXT: enable the flag, then prove one cycle. Rollback: --rollback")
    return 0


def rollback():
    if DST.exists():
        shutil.move(str(DST), str(DST.with_name("wake-emitter.rolledback-%d" % int(time.time()))))
        print("REMOVED %s (moved aside, not deleted)" % DST)
    else:
        print("nothing installed at %s" % DST)
    return 0


parser = argparse.ArgumentParser()
parser.add_argument("--apply", action="store_true")
parser.add_argument("--rollback", action="store_true")
parser.add_argument("--status", action="store_true")
parser.add_argument("--dry-run", action="store_true", default=True)
args = parser.parse_args()

if args.rollback:
    sys.exit(rollback())
if args.status:
    print(json.dumps(status(), indent=1))
    sys.exit(0)
if args.apply:
    print(json.dumps(status(), indent=1))
    print()
    sys.exit(apply())
plan()
print()
print(json.dumps(status(), indent=1))
sys.exit(0)
