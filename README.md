# W1 — Wake activation tool (rebuilt)

Rebuild of `scripts/wake_activate.py` plus the wake-emitter plugin defaults, fixing the
six blockers named by the release-gate review. Rehearsed only; **nothing live was installed,
deployed, or restarted**, and the real `~/.hermes/config.yaml` and `~/.hermes/plugins/` were
never written.

## Blocker status

| # | Blocker | Status | Proof |
|---|---|---|---|
| B1 | `--apply` writes an explicit config section and the flag reads back ON | **CLOSED** | `test_b1_apply_writes_explicit_config_section`; config gets `plugins.enabled += wake-emitter` and `plugins.entries.wake-emitter.settings.enabled: true`; `--status` reads `flag_state: ON` |
| B2 | Defaults that can never work | **CLOSED** | `plugins/wake-emitter/emitter.py` `build_from_ctx`: `lane` default `None` (resolve_origin refuses), all four path defaults absolute under `Path.home()/.hermes`; `test_emitter_defaults.py` proves an unconfigured install ends `BLOCKED_UNRESOLVED_ORIGIN` and never invokes a sender |
| B3 | Rollback that restores | **CLOSED** | `--rollback` restores the recorded backup, verifies `sha256(config) == pre-apply hash`, moves the plugin dir aside, verifies absence, and reports `verified/config_hash_matches/plugin_dir_absent`; second rollback is a no-op. `test_b3_...`, `test_rehearsal_...` |
| B4 | SHA pin | **CLOSED** | `git rev-parse HEAD` + clean-tree check; refuses on missing/wrong SHA or dirty tree before any write; record carries `source_sha`, per-file `file_hashes`, `installed_hashes`; installed hashes asserted equal. `test_b4_*` |
| B5 | Preconditions fail closed before any write | **CLOSED** | lane must resolve with both `conv` and `generation` (not retired); sender/pane_read/journal absolute; sender/pane_read exist; journal parent writable; `pane_read <lane>` must return `MATCHED_BY_CONV`; `wake-emitter` must not be in `plugins.disabled`. `test_b5_*` (7 cases) |
| B6 | Truthful status | **CLOSED** | `--status` reads the flag and reports `ON/OFF/DISABLED/UNKNOWN` plus `effective_settings` computed by the plugin's own `build_from_ctx`. `test_b6_*` |

### Seventh defect (found while fixing B1)

The plugin directory had **no `plugin.yaml`**, so Hermes discovery returned **zero manifests**
for it (`scan_directory` probe: `manifests: []`) — the plugin could never load, enabled or not.
Fixed by adding `plugins/wake-emitter/plugin.yaml` with `provides_hooks: [on_session_end]`.
Probe now returns `manifests: [('wake-emitter', 'wake-emitter', ['on_session_end'])]` and
`gate_manifest(...) == load` when `wake-emitter` is in `plugins.enabled`, `placeholder` when not.

## Files

- `scripts/wake_activate.py` — the tool (`--dry-run/--status/--apply/--rollback`).
- `plugins/wake-emitter/emitter.py` — plugin with fail-closed absolute defaults.
- `plugins/wake-emitter/plugin.yaml` — discovery manifest.
- `tests/test_wake_activate.py` — B1-B6 + sandbox rehearsal (subprocess against a fake HOME).
- `tests/test_emitter_defaults.py` — B2.
- `scripts/rehearse_activation.py` — standalone rehearsal transcript generator.
- `RED.txt` / `GREEN.txt` — test output. `rehearsal_transcript.txt` — full apply/status/rollback.
- `baseline/` — untouched upstream tool + plugin used as the RED reference.

## Run

```bash
PY="env -u PYTHONPATH C:/Users/wigmore/.hermes/hermes-agent/venv/Scripts/python.exe"

# RED (untouched baseline tool + plugin)
WAKE_TOOL="$PWD/baseline/wake_activate_old.py" WAKE_PLUGIN_DIR="$PWD/baseline/wake-emitter" \
  $PY -m pytest tests/ -q

# GREEN (this deliverable)
$PY -m pytest tests/ -q

# rehearsal transcript
$PY scripts/rehearse_activation.py
```

## Evidence pointers

- RED: `19 failed, 3 passed`, exit 1 (baseline tool/plugin).
- GREEN: `22 passed`, exit 0.
- Regression: the upstream wake-emitter suite run against this plugin copy — `54 passed, 1 skipped`.
- Real config round-trip: enabling on a **copy** of the real `config.yaml` produces a 23-line diff
  (one list entry + the plugin's settings block); no comments exist in the real file and none are lost.
- Real pane: `pane_read.py gpt6:assistant --json` → `MATCHED_BY_CONV 6b01a297`, exit 0 (read-only).
