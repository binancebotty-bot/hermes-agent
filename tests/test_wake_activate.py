"""Activation-tool acceptance tests: blockers B1-B6 plus the sandbox rehearsal.

Every test drives the tool as a subprocess against a disposable fake HOME.
Nothing under the real C:/Users/wigmore/.hermes is read for writing, and no
transport, credential, or agent is contacted.

Set WAKE_TOOL / WAKE_PLUGIN_DIR to point at a candidate tool+plugin pair.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
TOOL = Path(os.environ.get("WAKE_TOOL", str(ROOT / "scripts" / "wake_activate.py")))
PLUGIN_DIR = Path(os.environ.get("WAKE_PLUGIN_DIR", str(ROOT / "plugins" / "wake-emitter")))

LANE = "gpt6:assistant"
CONV = "https://chatgpt.com/c/6b01a297-292c-83ed-9f47-f49ab937c2f1"
JOURNAL = "event_outbox.jsonl"

FAKE_PANE_READ = (
    'import sys\n'
    'lane = sys.argv[1] if len(sys.argv) > 1 else ""\n'
    'if lane == "gpt6:unreachable":\n'
    '    print("NO_MATCH for %r. tabs seen:" % lane)\n'
    '    raise SystemExit(1)\n'
    'print("MATCHED_BY_CONV 6b01a297 (role %s; tab title lags)" % lane)\n'
    'print(\'{"url":"' + CONV + '","selector_chain":"main-div-group","count":1,'
    '"turns":[{"role":"unknown","text":"hello"}]}\')\n'
)

FAKE_CONFIG = """\
# fake hermes config for the activation rehearsal
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


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.name=rehearsal", "-c", "user.email=rehearsal@local", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    )


def build_home(tmp_path, *, lanes=None, pane_ok=True, sender_exists=True, config_text=None):
    """Create a disposable HOME with the exact layout the tool expects."""
    home = tmp_path / "home"
    repo = home / "oc-runs" / "w1-wake-w1"
    src = repo / "plugins" / "wake-emitter"
    src.mkdir(parents=True)
    for item in PLUGIN_DIR.iterdir():
        if item.is_file():
            shutil.copy2(item, src / item.name)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "rehearsal baseline")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()

    hermes = home / ".hermes"
    (hermes / "plugins").mkdir(parents=True)
    (hermes / "state").mkdir()
    (hermes / "scripts").mkdir()
    (hermes / "config.yaml").write_text(config_text or FAKE_CONFIG, encoding="utf-8")
    if lanes is None:
        lanes = {LANE: {"conv": CONV, "generation": 1}}
    (hermes / "state" / "peer_lane_map.json").write_text(
        json.dumps({"lanes": lanes}), encoding="utf-8")
    (hermes / "scripts" / "pane_read.py").write_text(FAKE_PANE_READ, encoding="utf-8")
    if not pane_ok:
        (hermes / "scripts" / "pane_read.py").write_text(
            'print("NO_MATCH for lane")\nraise SystemExit(1)\n', encoding="utf-8")
    if sender_exists:
        (hermes / "scripts" / "pane_send_guarded.py").write_text(
            "print('GUARD_COMPLETE key=x')\n", encoding="utf-8")
    (hermes / "state" / JOURNAL).write_text('{"sentinel": 1}\n', encoding="utf-8")

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    # H1: defaults now point at the reviewed artefact repo, so every rehearsal
    # must name its own fake source/repo explicitly.
    env["WAKE_SOURCE"] = str(src)
    env["WAKE_REPO"] = str(repo)
    env["WAKE_EXPECTED_SHA"] = head
    # H2: the live config SHA is now a mandatory, verified input.
    env["WAKE_EXPECTED_CONFIG_SHA"] = sha(hermes / "config.yaml")
    return SimpleNamespace(home=home, repo=repo, src=src, hermes=hermes, head=head,
                           env=env, config=hermes / "config.yaml",
                           dst=hermes / "plugins" / "wake-emitter",
                           record=hermes / "state" / "wake_activation.json",
                           journal=hermes / "state" / JOURNAL)


def run_tool(fx, args):
    return subprocess.run([sys.executable, str(TOOL), *args], env=fx.env,
                          capture_output=True, text=True, timeout=120)


def result(stdout):
    """Parse the tool's final `RESULT {...}` line, or None when absent."""
    for line in reversed(stdout.splitlines()):
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT "):])
    return None


def status_json(fx):
    proc = run_tool(fx, ["--status"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


# ---- B1: enable actually enables -------------------------------------------

def test_b1_apply_writes_explicit_config_section(tmp_path):
    fx = build_home(tmp_path)
    before = sha(fx.config)
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert result(proc.stdout)["flag_state"] == "ON"

    cfg = yaml.safe_load(fx.config.read_text(encoding="utf-8"))
    assert "wake-emitter" in cfg["plugins"]["enabled"]
    entry = cfg["plugins"]["entries"]["wake-emitter"]
    assert entry["settings"]["enabled"] is True
    assert entry["settings"]["lane"] == LANE
    assert entry["settings"]["sender_path"] == str(fx.hermes / "scripts" / "pane_send_guarded.py")
    assert entry["settings"]["pane_read_path"] == str(fx.hermes / "scripts" / "pane_read.py")
    assert entry["settings"]["journal_path"] == str(fx.hermes / "state" / JOURNAL)
    assert entry["settings"]["peer_lane_map_path"] == str(fx.hermes / "state" / "peer_lane_map.json")
    assert sha(fx.config) != before
    text = fx.config.read_text(encoding="utf-8")
    assert "# fake hermes config for the activation rehearsal" in text, "config comments were dropped"

    st = status_json(fx)
    assert st["flag_state"] == "ON"


# ---- B3: rollback restores -------------------------------------------------

def test_b3_rollback_restores_config_byte_identically(tmp_path):
    fx = build_home(tmp_path)
    pre = sha(fx.config)
    assert run_tool(fx, ["--apply"]).returncode == 0
    assert sha(fx.config) != pre

    # Drift after apply: an unrelated writer replaces the config.
    fx.config.write_text("plugins:\n  enabled: []\n", encoding="utf-8")
    proc = run_tool(fx, ["--rollback"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    res = result(proc.stdout)
    assert res["verified"] is True
    assert res["config_hash_matches"] is True
    assert res["plugin_dir_absent"] is True
    assert res["config_sha256"] == pre
    assert sha(fx.config) == pre
    assert not fx.dst.exists()

    # Second rollback is a harmless no-op.
    again = run_tool(fx, ["--rollback"])
    assert again.returncode == 0, again.stdout + again.stderr
    res2 = result(again.stdout)
    assert res2["noop"] is True
    assert res2["verified"] is True
    assert sha(fx.config) == pre
    assert not fx.dst.exists()


# ---- B4: SHA pin -----------------------------------------------------------

def test_b4_apply_pins_sha_and_per_file_hashes(tmp_path):
    fx = build_home(tmp_path)
    assert run_tool(fx, ["--apply"]).returncode == 0
    rec = json.loads(fx.record.read_text(encoding="utf-8"))
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=fx.repo,
                          capture_output=True, text=True, check=True).stdout.strip()
    assert rec["source_sha"] == head
    assert rec["source_sha"] == fx.head
    assert rec["dirty"] is False
    assert rec["expected_sha"] == fx.head
    assert rec["file_hashes"], "no per-file hashes recorded"
    assert rec["installed_hashes"] == rec["file_hashes"]
    assert rec["file_hashes"]["emitter.py"]


def test_b4_refuses_wrong_expected_sha(tmp_path):
    fx = build_home(tmp_path)
    pre = sha(fx.config)
    fx.env["WAKE_EXPECTED_SHA"] = "0" * 40
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode != 0
    assert not fx.dst.exists()
    assert sha(fx.config) == pre
    assert not fx.record.exists()


def test_b4_refuses_missing_expected_sha(tmp_path):
    fx = build_home(tmp_path)
    pre = sha(fx.config)
    fx.env.pop("WAKE_EXPECTED_SHA", None)
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode != 0
    assert not fx.dst.exists()
    assert sha(fx.config) == pre


def test_b4_refuses_dirty_working_tree(tmp_path):
    fx = build_home(tmp_path)
    pre = sha(fx.config)
    (fx.src / "uncommitted.txt").write_text("dirty", encoding="utf-8")
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode != 0
    assert not fx.dst.exists()
    assert sha(fx.config) == pre


def test_b4_refuses_non_git_source(tmp_path):
    fx = build_home(tmp_path)
    pre = sha(fx.config)
    # git object files are read-only on Windows; rename .git aside rather than rmtree.
    shutil.move(str(fx.repo / ".git"), str(fx.repo / ".git.disabled"))
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode != 0
    assert not fx.dst.exists()
    assert sha(fx.config) == pre
    assert not fx.record.exists()


# ---- B5: preconditions fail closed before any write ------------------------

@pytest.mark.parametrize("lanes", [
    {},
    {LANE: {"conv": CONV}},
    {LANE: {"generation": 1}},
    {LANE: {"conv": CONV, "generation": 1, "retired_unbound": {"reason": "retired"}}},
])
def test_b5_unresolvable_lane_refuses_before_write(tmp_path, lanes):
    fx = build_home(tmp_path, lanes=lanes)
    pre = sha(fx.config)
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode != 0
    assert sha(fx.config) == pre
    assert not fx.dst.exists()
    assert not fx.record.exists()


def test_b5_missing_sender_refuses_before_write(tmp_path):
    fx = build_home(tmp_path, sender_exists=False)
    pre = sha(fx.config)
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode != 0
    assert sha(fx.config) == pre
    assert not fx.dst.exists()


def test_b5_relative_sender_refuses_before_write(tmp_path):
    fx = build_home(tmp_path)
    fx.env["WAKE_SENDER"] = "scripts/pane_send_guarded.py"
    pre = sha(fx.config)
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode != 0
    assert sha(fx.config) == pre
    assert not fx.dst.exists()


def test_b5_unreadable_pane_refuses_before_write(tmp_path):
    fx = build_home(tmp_path, pane_ok=False)
    pre = sha(fx.config)
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode != 0
    assert sha(fx.config) == pre
    assert not fx.dst.exists()


def test_b5_disabled_list_refuses_before_write(tmp_path):
    fx = build_home(tmp_path, config_text=FAKE_CONFIG.replace(
        "  disabled:\n    - some-plugin\n", "  disabled:\n    - some-plugin\n    - wake-emitter\n"))
    pre = sha(fx.config)
    proc = run_tool(fx, ["--apply"])
    assert proc.returncode != 0
    assert sha(fx.config) == pre
    assert not fx.dst.exists()


# ---- B6: truthful status ---------------------------------------------------

def test_b6_status_reports_read_flag_and_effective_settings(tmp_path):
    fx = build_home(tmp_path)
    before = status_json(fx)
    assert before["flag_state"] == "OFF"
    assert before["effective_settings"]["lane"] is None

    assert run_tool(fx, ["--apply"]).returncode == 0
    after = status_json(fx)
    assert after["flag_state"] == "ON"
    eff = after["effective_settings"]
    assert eff["enabled"] is True
    assert eff["lane"] == LANE
    assert eff["sender_path"] == str(fx.hermes / "scripts" / "pane_send_guarded.py")
    assert eff["journal_path"] == str(fx.hermes / "state" / JOURNAL)


def test_b6_status_reports_disabled_when_listed_in_disabled(tmp_path):
    fx = build_home(tmp_path, config_text=FAKE_CONFIG.replace(
        "  disabled:\n    - some-plugin\n", "  disabled:\n    - some-plugin\n    - wake-emitter\n"))
    st = status_json(fx)
    assert st["flag_state"] == "DISABLED"


# ---- sandbox rehearsal -----------------------------------------------------

def test_rehearsal_apply_status_rollback(tmp_path):
    fx = build_home(tmp_path)
    pre = sha(fx.config)
    journal_before = fx.journal.read_bytes()

    applied = run_tool(fx, ["--apply"])
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert result(applied.stdout)["flag_state"] == "ON"
    assert fx.dst.exists()

    st = status_json(fx)
    assert st["flag_state"] == "ON"

    rolled = run_tool(fx, ["--rollback"])
    assert rolled.returncode == 0, rolled.stdout + rolled.stderr
    res = result(rolled.stdout)
    assert res["verified"] is True
    assert res["config_hash_matches"] is True
    assert res["plugin_dir_absent"] is True

    assert sha(fx.config) == pre, "config not restored byte-for-byte"
    assert not fx.dst.exists(), "plugin dir not removed"
    assert fx.journal.read_bytes() == journal_before, "journal was touched"

    again = run_tool(fx, ["--rollback"])
    assert again.returncode == 0
    assert result(again.stdout)["noop"] is True
    assert sha(fx.config) == pre
