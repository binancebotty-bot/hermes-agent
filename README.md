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

## Installer hardening (review findings H1-H5)

`scripts/wake_activate.py` now defaults `--source/--repo` to this reviewed artefact repo
(never the manifest-less `w1-wake-w1` tree) and requires both `--expected-sha` and the
new mandatory `--expected-config-sha` (or their `WAKE_*` env equivalents). Every refusal
happens before the first write.

| Finding | Fix | Proving test (`tests/test_hardening.py`) |
|---|---|---|
| H1 wrong default source | defaults point at `w1-activate-w1`; missing SHA refuses | `test_h1_defaults_point_to_reviewed_artefact_and_refuse_bare_apply` |
| H2 no config-hash check | live config SHA verified before any write | `test_h2_wrong_expected_config_sha_refuses_before_write` |
| H3 rollback clobbers first | backup hash verified **before** overwrite; both hashes reported | `test_h3_corrupt_backup_refuses_without_touching_live_config` |
| H4 no mid-apply recovery | `PRE` record written before first mutation, flipped to `applied` at the end; rollback accepts `PRE` | `test_h4_pre_record_written_before_first_mutation_and_rollback_recovers` |
| H5 destination not checked | plugins parent writability is a precondition | `test_h5_unwritable_plugins_destination_refuses_before_write` |

Controller additions: recovery is proven at all five interruption points
(`test_interrupted_apply_recovery_at_each_step`), config/source TOCTOU drift fails closed
(`test_toctou_drift_fails_closed`), and a post-install gate uses the **real** Hermes
`scan_directory`/`gate_manifest` plus the plugin's own `build_from_ctx` so a copied-but-
unloadable artefact cannot report success (`test_post_install_verification_*`).

The two rehearsal-only hooks are `WAKE_STOP_AFTER=<phase>` (simulate a crash after
`preconditions|backup|plugin|config|record`) and `WAKE_DRIFT=config|source` (simulate an
external writer between check and write). Both are inert unless explicitly set.

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

- RED (original baseline): `19 failed, 3 passed`, exit 1 (upstream tool/plugin).
- RED (H1-H5 + controller additions): `14 failed`, exit 1 — `RED_H1_H5.txt`.
- GREEN (this deliverable): `42 passed`, exit 0 — `GREEN_H1_H5.txt`.
- Regression: the upstream wake-emitter suite run against this plugin copy — `54 passed, 1 skipped`.
- Real config round-trip: enabling on a **copy** of the real `config.yaml` produces a 23-line diff
  (one list entry + the plugin's settings block); no comments exist in the real file and none are lost.
- Real pane: `pane_read.py gpt6:assistant --json` → `MATCHED_BY_CONV 6b01a297`, exit 0 (read-only).

## W1b — durable task binding and fail-closed delivery

The installed `turn_finalizer` emits no conversation or generation, so the emitter's single
globally configured lane sent every wake to the same conversation. W1b adds a durable,
gateway-free binding store in `plugins/wake-emitter/emitter.py` and resolves the send target
from it at send time.

**Binding schema** — one JSON-lines row per bind in `binding_path`
(default `~/.hermes/state/task_bindings.jsonl`):

```json
{"kind":"binding","task_id":"cron:abc:1","session_id":null,
 "conversation_id":"https://chatgpt.com/c/<id>","generation":3,
 "bound_utc":"2026-11-22T15:00:00Z","bound_by":"dispatcher"}
```

Keys are strict-equality (`task_id` first, `session_id` fallback); a later row for the same
key supersedes earlier ones. Writes are one fsynced append under an advisory + in-process
lock, so concurrent writers cannot lose or tear a row, and a fresh process reads the same
file with no gateway or daemon.

**Fail-closed rule:** a bound task is sent to its bound conversation/generation; the peer
map must still report that exact generation, or the send is refused as `STALE_GENERATION`
(never into a rotated pane). An unbound task records `UNBOUND_TASK` and sends nothing — it
is never silently routed to the global lane. Only `allow_unbound_global_fallback: true`
restores the old global-lane behaviour. When no `binding_path` is configured at all, the
legacy component path is unchanged. `UNBOUND_TASK` and `STALE_GENERATION` are terminal
outcomes, so they cannot be retried into a wrong conversation.

**Two defects fixed in the same analysis:**

- BUSY no-send: the write-ahead `ATTEMPTED` intent is now voided for a definitive BUSY/refused
  pane, so a stranded event retries on the next idle invocation instead of being reported as
  `RECONCILE_UNPROVEN` — `tests/test_binding.py::test_busy_pane_retries_on_next_invocation_instead_of_reconcile_unproven`.
- UNCERTAIN misclassification: a literal `UNCERTAIN:` sender line is normalised to
  `DELIVERY_PENDING` rather than a terminal outcome, so it is never a delivery proof and
  settles only through the existing pane-evidence sweep —
  `tests/test_binding.py::test_uncertain_sender_line_is_not_terminal_and_reconciles_as_pending`.

**Evidence:** RED `PYTEST_EXIT=1` (9 failed) — `RED_W1B.txt`; GREEN `PYTEST_EXIT=0`
(9 passed) — `GREEN_W1B.txt`; full suite `FULL_EXIT=0` (132 passed) — `GREEN_FULL_W1B.txt`.
