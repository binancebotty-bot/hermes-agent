#!/usr/bin/env python3
"""Wake-emitter activation tool.

Applies the reviewed wake-emitter plugin with an explicit, read-back-verified
config section, a pinned source SHA, fail-closed preconditions (including live
pane reachability), and a rollback that restores the config byte-for-byte and
verifies the restore.

Nothing here touches a live install unless --home points at the real Hermes
home. Every path is overridable (flag or WAKE_* env) so the whole cycle can be
rehearsed against a disposable fake home.

Usage:
    python wake_activate.py                 # dry-run plan + status
    python wake_activate.py --status        # JSON status only
    python wake_activate.py --apply         # preconditions -> pin -> install -> enable
    python wake_activate.py --rollback      # restore config, move plugin aside, verify
    python wake_activate.py --apply --rehearse   # ONLY for a disposable fake home

Rehearsal hooks (crash injection, drift injection) are inert unless --rehearse is
passed; an environment variable alone can never mutate anything. --rehearse is
refused against the real user home.

Exit codes: 0 success, 2 refused (no write), 1 rollback could not verify.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

PLUGIN_ID = "wake-emitter"
DEFAULT_LANE = "gpt6:assistant"
ACK_HORIZON_SECONDS = 1800.0

# Reviewed safety settings. A re-apply must establish these when absent and
# preserve operator values verbatim when present; dropping the scope reopens
# the conversation flood that only a hand-edited config had been holding back.
REVIEWED_SCOPE = {
    "require_task_id": True,
    "task_id_prefix_allow": ["cron:", "subagent-"],
    "task_id_exclude_prefixes": ["cron:8cff233194f6", "local-"],
}
REVIEWED_HOOK_BUDGET_SECONDS = 25

# Importing the plugin to read its effective defaults must not drop __pycache__
# into the source repo: that would dirty the tree the SHA pin is checking.
sys.dont_write_bytecode = True

# ruamel round-trip keeps the real config's comments and formatting; PyYAML is
# the fallback. Both are in the Hermes venv.
try:
    from ruamel.yaml import YAML as _RuamelYAML

    def _ruamel():
        handle = _RuamelYAML(typ="rt")
        handle.preserve_quotes = True
        handle.default_flow_style = False
        # Match the house style (indented sequences): a plain round-trip of the
        # real config.yaml is byte-identical with this indent.
        handle.indent(mapping=2, sequence=4, offset=2)
        return handle

    YAML_LIB = "ruamel"
except Exception:  # pragma: no cover - fallback path
    import yaml as _pyyaml  # noqa: F401

    YAML_LIB = "pyyaml"


def load_config(path):
    with open(path, encoding="utf-8") as handle:
        if YAML_LIB == "ruamel":
            return _ruamel().load(handle)
        import yaml
        return yaml.safe_load(handle)


def save_config(path, data):
    """Atomic replace: dump to a sibling temp file, then os.replace."""
    path = Path(path)
    descriptor, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                            dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            if YAML_LIB == "ruamel":
                _ruamel().dump(data, handle)
            else:
                import yaml
                yaml.safe_dump(data, handle, sort_keys=False)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def tree_hashes(root):
    """SHA-256 of every real file in the plugin tree, relative POSIX path key."""
    out = {}
    root = Path(root)
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if "__pycache__" in rel or rel.endswith(".pyc"):
            continue
        out[rel] = sha256(path)
    return out


# -- config flag state ------------------------------------------------------

def read_flag(config, plugin_id=PLUGIN_ID):
    """The flag state actually read from the config, never a guess."""
    if not isinstance(config, dict):
        return {"flag_state": "UNKNOWN", "in_enabled_list": False,
                "in_disabled_list": False, "settings_enabled": False}
    plugins = config.get("plugins") or {}
    enabled = plugins.get("enabled")
    disabled = plugins.get("disabled") or []
    entry = (plugins.get("entries") or {}).get(plugin_id) or {}
    settings = entry.get("settings") or {}
    legacy = entry.get("config") or {}
    flag = settings.get("enabled", legacy.get("enabled", False))
    in_enabled = isinstance(enabled, list) and plugin_id in enabled
    in_disabled = isinstance(disabled, list) and plugin_id in disabled
    if in_disabled:
        state = "DISABLED"
    elif in_enabled and bool(flag):
        state = "ON"
    else:
        state = "OFF"
    return {"flag_state": state, "in_enabled_list": in_enabled,
            "in_disabled_list": in_disabled, "settings_enabled": bool(flag)}


class _Ctx:
    """Mirrors PluginContext.get_config: settings subtree, then legacy config."""

    def __init__(self, config, plugin_id):
        self._config = config or {}
        self._id = plugin_id
        self.logger = None

    def get_config(self, key, default=None):
        plugins = self._config.get("plugins") or {}
        entry = (plugins.get("entries") or {}).get(self._id) or {}
        settings = entry.get("settings") or {}
        if isinstance(settings, dict) and key in settings:
            return settings[key]
        legacy = entry.get("config") or {}
        if isinstance(legacy, dict) and key in legacy:
            return legacy[key]
        return default


def effective_settings(config, source):
    """The settings build_from_ctx would actually use, from the plugin itself."""
    if not isinstance(config, dict) or not Path(source).exists():
        return None
    try:
        sys.path.insert(0, str(Path(source).parent))
        emitter = importlib.import_module(f"{PLUGIN_ID}.emitter")
        worker = emitter.build_from_ctx(_Ctx(config, PLUGIN_ID))
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "enabled": bool(worker.enabled),
        "lane": worker.lane,
        "journal_path": str(worker.journal_path),
        "peer_lane_map_path": str(worker.peer_map_path),
        "sender_path": worker.sender_path,
        "pane_read_path": worker.pane_read_path,
        "ack_horizon_seconds": worker.ack_horizon_seconds,
    }


def _discovery():
    """Import Hermes's real discovery/gate API, self-located under the venv."""
    try:
        root = Path(sys.prefix).parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from hermes_cli import plugins_discovery as discovery
        return discovery
    except Exception:
        return None


def verify_install(paths, config):
    """Post-install gate: the artefact must actually LOAD, not merely exist."""
    problems = []
    gate_action = None
    discovery = _discovery()
    if discovery is None:
        problems.append("Hermes discovery API unavailable")
    else:
        try:
            manifests = [m for m in discovery.scan_directory(paths.dst.parent, "user")
                         if discovery.manifest_key(m) == PLUGIN_ID]
            if len(manifests) != 1:
                problems.append(f"discovery found {len(manifests)} manifests for {PLUGIN_ID}")
            else:
                gate = discovery.gate_manifest(manifests[0], set(), {PLUGIN_ID})
                gate_action = gate.action
                if gate.action != "load":
                    problems.append(f"discovery gate action={gate.action!r} error={gate.error!r}")
        except Exception as exc:
            problems.append(f"discovery check failed: {type(exc).__name__}: {exc}")

    settings = effective_settings(config, paths.dst)
    effective_ok = False
    if not settings or settings.get("error"):
        problems.append(f"installed plugin config path unusable: {settings}")
    else:
        effective_ok = (
            settings["enabled"] is True
            and settings["lane"] == paths.lane
            and all(Path(settings[key]).is_absolute() for key in
                    ("journal_path", "peer_lane_map_path", "sender_path", "pane_read_path"))
        )
        if not effective_ok:
            problems.append(f"installed plugin settings not effective: {settings}")
    return {
        "ok": not problems, "problems": problems,
        "manifest_gate": gate_action,
        "lane": settings.get("lane") if settings else None,
        "paths_absolute": effective_ok,
        "enabled": bool(settings and settings.get("enabled")),
    }


# -- origin resolution (fail closed, mirrors emitter.resolve_origin) --------

def resolve_origin(peer_map, lane):
    if not lane:
        return None
    entry = (peer_map.get("lanes") or {}).get(lane)
    if not isinstance(entry, dict):
        return None
    if entry.get("retired_unbound") or entry.get("retired") is True or entry.get("state") == "RETIRED":
        return None
    conv = entry.get("conv")
    generation = entry.get("generation")
    if not conv or generation is None:
        return None
    return {"conv": conv, "generation": generation}


# -- preconditions ----------------------------------------------------------

def _nearest_existing(path):
    path = Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _dir_writable(path):
    """Fail closed when the plugins destination is missing or not writable."""
    path = Path(path)
    if not path.is_dir():
        return False
    try:
        if not (path.stat().st_mode & stat.S_IWRITE):
            return False
    except OSError:
        return False
    return os.access(str(path), os.W_OK)


def preconditions(paths):
    """Every check runs before any write. Returns a list of refusal reasons."""
    errors = []
    if not paths.source.is_dir():
        errors.append(f"source plugin missing: {paths.source}")

    peer_map = None
    if not paths.peer_map.is_absolute():
        errors.append(f"peer_lane_map_path not absolute: {paths.peer_map}")
    if not paths.peer_map.is_file():
        errors.append(f"peer_lane_map not found: {paths.peer_map}")
    else:
        try:
            peer_map = json.loads(paths.peer_map.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"peer_lane_map unreadable: {type(exc).__name__}: {exc}")
    if peer_map is not None:
        origin = resolve_origin(peer_map, paths.lane)
        if origin is None:
            errors.append(
                f"lane {paths.lane!r} not resolvable in {paths.peer_map}: "
                "needs both conv and generation, not retired")
        else:
            print(f"  lane {paths.lane}: conv {origin['conv']} gen {origin['generation']}")

    for label, path in (("sender", paths.sender), ("pane_read", paths.pane_read)):
        if not path.is_absolute():
            errors.append(f"{label} path not absolute: {path}")
        if not path.is_file():
            errors.append(f"{label} not found: {path}")
    if not paths.journal.is_absolute():
        errors.append(f"journal path not absolute: {paths.journal}")
    parent = paths.journal.parent
    if parent.is_dir():
        if not os.access(str(parent), os.W_OK):
            errors.append(f"journal parent not writable: {parent}")
    else:
        anchor = _nearest_existing(parent)
        if not os.access(str(anchor), os.W_OK):
            errors.append(f"journal parent not creatable: {parent} (anchor {anchor} not writable)")

    if not _dir_writable(paths.dst.parent):
        errors.append(f"plugins destination not writable: {paths.dst.parent}")

    config = load_config(paths.config) if paths.config.is_file() else None
    if config is None:
        errors.append(f"config missing: {paths.config}")
    else:
        disabled = (config.get("plugins") or {}).get("disabled") or []
        if isinstance(disabled, list) and PLUGIN_ID in disabled:
            errors.append(f"{PLUGIN_ID} is listed in plugins.disabled; remove it first")

    ok, matched, output = run_pane_read(paths.pane_read, paths.lane)
    if not ok or not matched:
        errors.append(f"pane not reachable at activation time: pane_read {paths.lane} "
                      f"returned rc={'0' if ok else 'nonzero'}, MATCHED_BY_CONV={matched}")
        tail = (output or "").strip().splitlines()[-3:]
        for line in tail:
            print(f"    pane_read: {line}")
    else:
        print("  pane reachable: MATCHED_BY_CONV")
    return errors


# -- git --------------------------------------------------------------------

def git_head(repo):
    proc = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def git_toplevel(repo):
    proc = subprocess.run(["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    try:
        return str(Path(proc.stdout.strip()).resolve())
    except OSError:
        return None


def git_dirty(repo):
    proc = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    return bool(proc.stdout.strip())


# -- pane reachability ------------------------------------------------------

def run_pane_read(pane_read, lane, timeout=60):
    if not Path(pane_read).is_file():
        return False, False, f"pane_read not found: {pane_read}"
    argv = [sys.executable, str(pane_read), lane, "--json"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return False, False, f"{type(exc).__name__}: {exc}"
    stdout = proc.stdout or ""
    matched = any(line.strip().startswith("MATCHED_BY_CONV") for line in stdout.splitlines())
    return proc.returncode == 0, matched, f"{stdout}\n{proc.stderr or ''}"


# -- config mutation --------------------------------------------------------

def reviewed_python_executable(paths):
    """Caller-supplied interpreter, else this interpreter when it is a venv.

    Outside a venv there is no reviewed interpreter to pin, so the key stays
    absent and the plugin keeps its historical sys.executable fallback.
    """
    explicit = getattr(paths, "python_executable", None)
    if explicit:
        return explicit
    if sys.prefix != sys.base_prefix:
        return sys.executable
    return None


def enable_in_config(config, paths):
    plugins = config.setdefault("plugins", {})
    enabled = plugins.get("enabled")
    if not isinstance(enabled, list):
        enabled = []
        plugins["enabled"] = enabled
    if PLUGIN_ID not in enabled:
        enabled.append(PLUGIN_ID)
    entry = plugins.setdefault("entries", {}).setdefault(PLUGIN_ID, {})
    settings = entry.setdefault("settings", {})
    settings["enabled"] = True
    settings["lane"] = paths.lane
    settings["journal_path"] = str(paths.journal)
    settings["peer_lane_map_path"] = str(paths.peer_map)
    settings["sender_path"] = str(paths.sender)
    settings["pane_read_path"] = str(paths.pane_read)
    settings["ack_horizon_seconds"] = ACK_HORIZON_SECONDS
    # Fill only when absent: never rewrite an operator's reviewed values, and
    # never drop keys this tool does not own. Scope is the flood gate, so no
    # apply path may leave it unset on a fresh or previously hand-edited config.
    if "scope" not in settings:
        settings["scope"] = copy.deepcopy(REVIEWED_SCOPE)
    if "hook_budget_seconds" not in settings:
        settings["hook_budget_seconds"] = REVIEWED_HOOK_BUDGET_SECONDS
    python_executable = reviewed_python_executable(paths)
    if python_executable is not None and "python_executable" not in settings:
        settings["python_executable"] = python_executable
    return config


def read_record(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_record(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                            dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=1, sort_keys=True)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# -- commands ---------------------------------------------------------------

def _stop_after(phase, rehearse=False):
    """Rehearsal-only crash injection: simulate a death just after a phase."""
    if not rehearse:
        return
    if os.environ.get("WAKE_STOP_AFTER") == phase:
        print(f"SIMULATED DEATH after {phase}")
        raise SystemExit(3)


def _inject_drift(kind, paths, rehearse=False):
    """Rehearsal-only external writer: mutate a file between check and write."""
    if not rehearse:
        return
    if os.environ.get("WAKE_DRIFT") != kind:
        return
    if kind == "source":
        with open(paths.source / "emitter.py", "a", encoding="utf-8") as handle:
            handle.write("\n# external source drift during apply\n")
    elif kind == "config":
        with open(paths.config, "a", encoding="utf-8") as handle:
            handle.write("\n# external config drift during apply\n")


def apply(paths, expected_sha=None, expected_config_sha=None, rehearse=False):
    expected_sha = expected_sha or os.environ.get("WAKE_EXPECTED_SHA")
    expected_config_sha = expected_config_sha or os.environ.get("WAKE_EXPECTED_CONFIG_SHA")
    errors = preconditions(paths)
    if errors:
        for error in errors:
            print(f"REFUSED: {error}")
        return 2
    _stop_after("preconditions", rehearse)

    config_before = sha256(paths.config)
    if not expected_config_sha:
        print("REFUSED: no expected config SHA supplied (pass --expected-config-sha "
              "or set WAKE_EXPECTED_CONFIG_SHA)")
        return 2
    if expected_config_sha != config_before:
        print(f"REFUSED: expected config SHA {expected_config_sha} != live config SHA {config_before}")
        return 2

    toplevel = git_toplevel(paths.repo)
    if toplevel is None:
        print(f"REFUSED: source repo is not a git repository: {paths.repo}")
        return 2
    if Path(toplevel) != Path(paths.repo).resolve():
        print(f"REFUSED: {paths.repo} is not a git worktree root (git resolved {toplevel})")
        return 2
    head = git_head(paths.repo)
    if head is None:
        print(f"REFUSED: git rev-parse HEAD failed in {paths.repo}")
        return 2
    dirty = git_dirty(paths.repo)
    if dirty is None:
        print(f"REFUSED: git status failed in {paths.repo}")
        return 2
    if dirty:
        print(f"REFUSED: working tree dirty in {paths.repo}; commit before apply")
        return 2
    if not expected_sha:
        print("REFUSED: no expected SHA supplied (pass --expected-sha or set WAKE_EXPECTED_SHA)")
        return 2
    if expected_sha != head:
        print(f"REFUSED: expected SHA {expected_sha} != repo HEAD {head}")
        return 2

    source_hashes = tree_hashes(paths.source)
    if not source_hashes:
        print(f"REFUSED: source plugin has no files: {paths.source}")
        return 2

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = paths.config.with_name(f"{paths.config.name}.wake-backup-{stamp}")
    # PRE-RECORD before the first mutation: any later death is recoverable.
    record = {
        "utc": utc_now(), "action": "apply", "state": "pre", "plugin_id": PLUGIN_ID,
        "source_dir": str(paths.source), "repo": str(paths.repo),
        "source_sha": head, "expected_sha": expected_sha, "dirty": False,
        "expected_config_sha": expected_config_sha, "file_hashes": source_hashes,
        "config_path": str(paths.config), "config_sha256_before": config_before,
        "backup_path": str(backup), "plugin_dir": str(paths.dst),
        "lane": paths.lane, "intended_state": "ON",
    }
    write_record(paths.record, record)
    print(f"  pre-record: {paths.record} (state=pre; rollback recovers any later step)")

    shutil.copy2(paths.config, backup)
    if sha256(backup) != config_before:
        print("REFUSED: config backup does not match the pre-apply bytes")
        rollback(paths)
        return 2
    print(f"  backup: {backup}")
    _stop_after("backup", rehearse)

    # Re-hash the source immediately before the copy: refuse on TOCTOU drift.
    _inject_drift("source", paths, rehearse)
    if tree_hashes(paths.source) != source_hashes:
        print("REFUSED: source tree drifted between hash check and plugin copy; reverting")
        rollback(paths)
        return 2

    replaced = None
    if paths.dst.exists():
        replaced = paths.dst.with_name(f"{PLUGIN_ID}.replaced-{stamp}")
        shutil.move(str(paths.dst), str(replaced))
        print(f"  moved existing install aside: {replaced}")
    shutil.copytree(paths.source, paths.dst)
    installed = tree_hashes(paths.dst)
    if installed != source_hashes:
        shutil.rmtree(paths.dst, ignore_errors=True)
        if replaced is not None:
            shutil.move(str(replaced), str(paths.dst))
        print("REFUSED: installed file hashes do not match the source; copy reverted")
        return 2
    print(f"  installed {paths.dst} ({len(installed)} files, hashes match source)")
    _stop_after("plugin", rehearse)

    # Re-hash the live config immediately before the write: refuse on TOCTOU drift.
    _inject_drift("config", paths, rehearse)
    if sha256(paths.config) != config_before:
        print("REFUSED: live config drifted between hash check and write; reverting")
        rollback(paths)
        return 2

    config = load_config(paths.config)
    enable_in_config(config, paths)
    save_config(paths.config, config)
    readback = read_flag(load_config(paths.config))
    if readback["flag_state"] != "ON":
        print(f"REFUSED: config readback flag_state={readback['flag_state']!r}; reverting")
        rollback(paths)
        return 2
    print(f"  config enabled; readback flag_state=ON ({YAML_LIB} round-trip)")
    _stop_after("config", rehearse)

    # No inert-but-green install: discovery must see a loadable manifest and the
    # plugin's own config path must report the intended state.
    verification = verify_install(paths, load_config(paths.config))
    if not verification["ok"]:
        for problem in verification["problems"]:
            print(f"REFUSED: post-install verification: {problem}")
        rollback(paths)
        return 2
    print(f"  post-install verification: loadable (gate={verification['manifest_gate']}, "
          f"lane={verification['lane']}, paths_absolute={verification['paths_absolute']})")

    record["state"] = "applied"
    record["flag_state"] = "ON"
    record["installed_hashes"] = installed
    record["post_install_verification"] = verification
    write_record(paths.record, record)  # flip PRE -> COMPLETE
    _stop_after("record", rehearse)
    print(f"  record: {paths.record}")
    print(f"  pinned sha {head}")
    print("RESULT " + json.dumps({"action": "apply", "state": "applied",
                                 "flag_state": "ON", "source_sha": head,
                                 "files": len(installed), "verified": True,
                                 "verification": verification}))
    return 0


def rollback(paths):
    record = read_record(paths.record)
    if record and record.get("state") == "rolled_back":
        print("  nothing to roll back: record state is already rolled_back")
        print("RESULT " + json.dumps({"action": "rollback", "noop": True,
                                     "state": "rolled_back", "verified": True,
                                     "config_hash_matches": True,
                                     "plugin_dir_absent": not paths.dst.exists()}))
        return 0
    if not record or not record.get("backup_path"):
        print("REFUSED: no activation record with a config backup; cannot verify a restore")
        print("RESULT " + json.dumps({"action": "rollback", "verified": False,
                                     "reason": "no activation record",
                                     "plugin_dir_absent": not paths.dst.exists()}))
        return 1

    backup = Path(record["backup_path"])
    expected = record.get("config_sha256_before")
    if not backup.is_file():
        print(f"REFUSED: recorded backup missing: {backup}")
        print("RESULT " + json.dumps({"action": "rollback", "verified": False,
                                     "reason": "backup missing",
                                     "plugin_dir_absent": not paths.dst.exists()}))
        return 1

    # Verify the backup BEFORE touching the live config: a tampered backup must
    # never clobber it. Report both hashes so the operator can see the mismatch.
    backup_sha = sha256(backup)
    if expected is None or backup_sha != expected:
        print(f"REFUSED: backup hash {backup_sha} != recorded pre-apply hash {expected}; "
              "live config left untouched")
        print("RESULT " + json.dumps({"action": "rollback", "verified": False,
                                     "config_hash_matches": False,
                                     "backup_sha256": backup_sha, "expected_sha256": expected,
                                     "live_config_unchanged": True,
                                     "plugin_dir_absent": not paths.dst.exists()}))
        return 1

    if record.get("state") == "pre":
        print("  rolling back an interrupted apply (record state=pre)")
    shutil.copy2(backup, paths.config)
    config_ok = sha256(paths.config) == expected
    if not config_ok:
        print(f"REFUSED: restored config hash does not equal the pre-apply hash {expected}")
        print("RESULT " + json.dumps({"action": "rollback", "verified": False,
                                     "config_hash_matches": False,
                                     "plugin_dir_absent": not paths.dst.exists()}))
        return 1
    print(f"  config restored from {backup}")

    moved = None
    if paths.dst.exists():
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        moved = paths.dst.with_name(f"{PLUGIN_ID}.rolledback-{stamp}")
        shutil.move(str(paths.dst), str(moved))
        print(f"  plugin moved aside: {moved}")
    plugin_absent = not paths.dst.exists()
    verified = config_ok and plugin_absent

    record["state"] = "rolled_back"
    record["rollback"] = {
        "utc": utc_now(), "verified": verified, "config_hash_matches": config_ok,
        "plugin_dir_absent": plugin_absent, "config_sha256": sha256(paths.config),
        "plugin_moved_to": str(moved) if moved else None,
    }
    write_record(paths.record, record)
    print("RESULT " + json.dumps({"action": "rollback", "state": "rolled_back",
                                 "verified": verified, "config_hash_matches": config_ok,
                                 "plugin_dir_absent": plugin_absent,
                                 "config_sha256": sha256(paths.config)}))
    return 0 if verified else 1


def status(paths):
    config = load_config(paths.config) if paths.config.is_file() else None
    flag = read_flag(config)
    record = read_record(paths.record)
    out = {
        "utc": utc_now(), "plugin_id": PLUGIN_ID, "lane": paths.lane,
        "flag_state": flag["flag_state"],
        "flag": flag,
        "effective_settings": effective_settings(config, paths.source),
        "config": {"path": str(paths.config), "present": config is not None},
        "plugin": {
            "source_present": paths.source.is_dir(),
            "installed_present": paths.dst.is_dir(),
            "installed_hashes": tree_hashes(paths.dst),
        },
        "activation_record": record,
        "yaml_lib": YAML_LIB,
    }
    return out


# -- paths ------------------------------------------------------------------

def _real_user_home():
    """Operator home, ignoring the HOME/USERPROFILE redirect a rehearsal sets.

    HOMEDRIVE/HOMEPATH are not touched by the fake-HOME harness, so they name
    the real user home even when the rehearsal env points elsewhere.
    """
    drive = os.environ.get("HOMEDRIVE")
    path_part = os.environ.get("HOMEPATH")
    if drive and path_part:
        return Path(drive + path_part)
    return Path.home()


def build_paths(home=None, source=None, repo=None, lane=None):
    # ``home`` is the user home (Path.home() by default); the Hermes tree is
    # always ``<home>/.hermes`` so the fake-HOME rehearsal matches production.
    user_home = Path(home or os.environ.get("WAKE_HOME") or Path.home())
    hermes = Path(os.environ.get("WAKE_HERMES_HOME") or (user_home / ".hermes"))
    source = Path(source or os.environ.get("WAKE_SOURCE")
                  or (user_home / "oc-runs" / "w1-activate-w1" / "plugins" / PLUGIN_ID))
    repo = Path(repo or os.environ.get("WAKE_REPO")
                or (user_home / "oc-runs" / "w1-activate-w1"))
    lane = lane or os.environ.get("WAKE_LANE") or DEFAULT_LANE
    return SimpleNamespace(
        home=user_home, hermes=hermes, source=source, repo=repo, lane=lane,
        config=hermes / "config.yaml",
        dst=hermes / "plugins" / PLUGIN_ID,
        record=hermes / "state" / "wake_activation.json",
        peer_map=Path(os.environ.get("WAKE_PEER_MAP") or (hermes / "state" / "peer_lane_map.json")),
        sender=Path(os.environ.get("WAKE_SENDER") or (hermes / "scripts" / "pane_send_guarded.py")),
        pane_read=Path(os.environ.get("WAKE_PANE_READ") or (hermes / "scripts" / "pane_read.py")),
        journal=Path(os.environ.get("WAKE_JOURNAL") or (hermes / "state" / "event_outbox.jsonl")),
        python_executable=os.environ.get("WAKE_PYTHON_EXECUTABLE"),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Activate the wake-emitter plugin safely.")
    parser.add_argument("--home", help="Hermes home (contains config.yaml); default Path.home()")
    parser.add_argument("--source", help="plugin source directory")
    parser.add_argument("--repo", help="git repo root of the plugin source")
    parser.add_argument("--lane", help="target lane in state/peer_lane_map.json")
    parser.add_argument("--python-executable",
                        help="interpreter for the emitter's child processes; default: this "
                             "interpreter when inside a venv (or WAKE_PYTHON_EXECUTABLE)")
    parser.add_argument("--expected-sha", help="required source commit SHA to pin")
    parser.add_argument("--expected-config-sha",
                        help="required SHA-256 of the live config before any write")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--rehearse", action="store_true",
                        help="enable crash/drift injection; disposable fake homes only")
    args = parser.parse_args(argv)

    paths = build_paths(args.home, args.source, args.repo, args.lane)
    if args.python_executable:
        paths.python_executable = args.python_executable

    # Refuse before ANY command (including status/rollback) touches a real home.
    if args.rehearse:
        real_home = _real_user_home()
        if Path(paths.home).resolve() == real_home.resolve():
            print(f"REFUSED: REHEARSE_REAL_HOME --rehearse targets the real home "
                  f"{real_home}; use a disposable fake home")
            return 2

    if args.rollback:
        return rollback(paths)
    if args.status:
        print(json.dumps(status(paths), indent=1, sort_keys=True))
        return 0
    if args.apply:
        return apply(paths, args.expected_sha, args.expected_config_sha, args.rehearse)

    print("PLAN (dry run; no writes)")
    print(f"  home       {paths.home}")
    print(f"  source     {paths.source}")
    print(f"  repo       {paths.repo}")
    print(f"  lane       {paths.lane}")
    print(f"  config     {paths.config}")
    print(f"  plugin dst {paths.dst}")
    print("  1 preconditions: lane resolves; sender/pane_read exist; pane_read reports"
          " MATCHED_BY_CONV; plugins destination writable")
    print("  2 pin: git HEAD == --expected-sha, clean tree; live config == --expected-config-sha")
    print("  3 write PRE-RECORD, then backup config and copy plugin (re-hash source before copy)")
    print("  4 re-hash config before write; enable flag; read back flag_state=ON")
    print("  5 post-install gate: discovery gate=load and installed plugin reports the"
          " intended lane and absolute paths; then flip record PRE -> COMPLETE")
    print("  6 rollback: verify backup hash BEFORE overwrite, restore config, remove plugin")
    print()
    print("RESULT " + json.dumps(status(paths), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
