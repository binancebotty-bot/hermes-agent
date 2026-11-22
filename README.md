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

## W1d — three fail-closed ACK gates

W1c bound ACK reconciliation to the delivered conversation/generation but left three
fail-open holes. All three are now closed in `plugins/wake-emitter/emitter.py`; the
`baseline/` tree is untouched and no live install, config, gateway, cron or
conversation was written.

1. **Generation absence** — a bound event whose conversation has no peer-map entry
   records `ACK_GENERATION_UNKNOWN` and is never settled on absence. It stays
   retryable, so a later peer-map entry can settle it. A mismatched generation still
   records `ACK_GENERATION_ROLLED` (terminal), as before.
2. **Time binding** — an ACK only settles when its turn carries a UTC timestamp at
   or after the recorded delivery utc, allowing a named skew tolerance
   `ack_skew_seconds` (default `ACK_SKEW_SECONDS = 30.0`, operator-settable via
   plugin settings and reported by `--status`). A missing timestamp is
   `ACK_TIME_UNPROVEN`; a timestamp beyond the tolerance is `ACK_BEFORE_DELIVERY`.
   Both are retryable refusals, never settlements. W1e narrows this strict rule
   to readbacks that actually supply turn timestamps (see W1e below).
3. **Window truncation** — a reply found without its delivery turn is no longer
   accepted from the tail alone. One bounded wider read (`ACK_REREAD_WINDOW = 400`,
   `--window`) is attempted inside the existing ACK sweep deadline **only when the
   configured readback advertises `--window`** (W1e capability detection); a tool
   without that flag is never passed it. If the delivery turn is still absent, the
   outcome is `ACK_DELIVERY_UNPROVEN`. The pane record's `utc` is preserved by
   `_pane_turns` when the tool supplies one, so capable readbacks can bind time.

`_read_pane_turns` still has two production callers (the ACK sweep and
`_resume_event`); only the ACK sweep uses the wider-window argument. Graphify
rebuilt from the reviewed tree: 647 nodes / 1669 edges, new helpers `_turn_utc`
(1 production caller) and `_parse_utc` (2 production callers) present.

### Which ACK cases are PROVEN vs UNPROVEN

**PROVEN — may settle `DELIVERED_ACKED`:** an ACK in the event's own bound
conversation (or a configured-lane global-fallback event), with the delivery turn
present in the readback window, the peer map holding the conversation at the frozen
generation, and an ACK turn timestamp at/after the delivery utc minus
`ack_skew_seconds`. Proved by `test_a_...` / `test_f_...` in `tests/test_ack_binding.py`,
`test_a_pending_...` / `test_hook_invocation_...` in `tests/test_pending_reconcile.py`,
and W1d case (c). Their assertions are unchanged; only the fixtures were upgraded
to carry the delivery turn and turn timestamp the new gates require. The recorded
row now also carries `ack_binding: "time_verified"` so this path is auditable apart
from order-only settlement.

**UNPROVEN / REFUSED — never settle:** peer-map entry absent
(`ACK_GENERATION_UNKNOWN`, retryable); in a timestamp-supplying readback, an ACK
timestamp before delivery beyond skew (`ACK_BEFORE_DELIVERY`) or a missing ACK
timestamp (`ACK_TIME_UNPROVEN`); an ACK ahead of the delivery turn in turn order
(`ACK_BEFORE_DELIVERY`); delivery turn missing from the window
(`ACK_DELIVERY_UNPROVEN`, with the wider read only when supported); generation
rolled after send (`ACK_GENERATION_ROLLED`); ACK in a different conversation, or a
reply without the nonce (W1c cases (b)/(c)/(d), unchanged). All remain open to a
later, better readback except the terminal `ACK_GENERATION_ROLLED`.

**Evidence:** RED `PYTEST_EXIT=1` (6 failed, each named assertion in
`RED_W1D.txt`), GREEN `PYTEST_EXIT=0` (6 passed) — `GREEN_W1D_NEW.txt`; full suite
`PYTEST_EXIT=0` (144 passed = 138 pre-existing + 6 new) — `GREEN_FULL_W1D.txt`.
Adversarial re-run of the new tests against the untouched `baseline/wake-emitter`
also fails 6/6 (`PYTEST_EXIT=1`) — `RED_W1D_BASELINE.txt`.

## W1e — capability-aware ACK binding (timestamp-less readbacks settle, labelled)

W1d assumed every ACK turn carries a wall-clock timestamp. The live
`~/.hermes/scripts/pane_read.py` supplies **no per-turn timestamp** (every turn
prints as `[unknown] ...`), and no `--window` flag. A strict time gate there would
record `ACK_TIME_UNPROVEN` forever for a delivery that actually landed and was
answered, destroying the ACK signal. W1e detects the readback's capability instead
of silently weakening or permanently refusing:

| Readback capability | Evidence required | Recorded outcome |
|---|---|---|
| Turn timestamps present | W1d strict path: ACK time at/after delivery utc minus `ack_skew_seconds` | `DELIVERED_ACKED`, `ack_binding: "time_verified"` |
| No turn timestamps, delivery turn present, ACK after it in order, nonce on ACK | order + nonce + delivery anchor (no wall clock claimed) | **`ACKED_ORDER_ONLY`**, `ack_binding: "order_only"` |
| No turn timestamps, delivery turn absent | delivery anchor missing -> no order to trust | `ACK_DELIVERY_UNPROVEN` (refuse, retryable) |
| No turn timestamps, ACK ahead of delivery turn in order | order violated | `ACK_BEFORE_DELIVERY` (refuse, retryable) |
| Readback advertises `--window` | one bounded wider read inside the existing sweep deadline, as before | preserved |
| Readback does not advertise `--window` | no re-read; never pass the unknown flag | same refusals/settlements from the one read |

**What `ACKED_ORDER_ONLY` means to an operator:** the event settled because the
delivered wake turn and a later nonce-bearing ACK were both present in the same
readback, in that order — *not* because wall-clock time was verified. It is a
terminal, auditable settlement with a distinct label; it is never reported as a
time-verified `DELIVERED_ACKED`. The two paths are distinguishable by both the
`outcome` and the `ack_binding` field on the outcome row.

One sentence on fields: the strict gate **requires** per-turn `utc`/`timestamp` on
the delivery and ACK turns (and the frozen delivery `utc` from the write-ahead
attempt row); the degraded gate **degrades over** wall-clock timestamps and
requires only turn text order plus the delivery anchor and the event nonce, while
`--window` is used only when the readback advertises it.

Everything already proven stays: `UNBOUND_TASK`/`STALE_GENERATION` delivery
fail-closed, cross-conversation ACK refusal, ACK-before-delivery in turn order,
nonce requirement, rolled generation, duplicate suppression, scope, hook budget,
staleness horizon and restart survival. Only `tests/test_w1d_fail_closed.py` case
(e) was adapted: it asserted an unconditional `--window` re-read, which
requirement 5 deliberately replaces with capability detection; its new form proves
both the no-flag skip and the preserved bounded wider read for a capable tool.

**Evidence:** RED `PYTEST_EXIT=1` (5 failed on the exact `daa1b12` base, byte-identical
test file) — `RED_W1E_CORRECTED.txt`; GREEN `PYTEST_EXIT=0` (5 passed) —
`GREEN_W1E.txt`; full suite `PYTEST_EXIT=0` (149 passed = 144 existing + 5 new) —
`GREEN_FULL_W1E.txt`.

## W1f — why only cron ever woke the lane, and the one admission that fixes it

Measured: every row in the live journal is `cron:` (plus two hand-made fixtures).
Two independent causes, neither in the ACK logic:

1. **Registration is per process, once.** `hermes_cli.plugins._delivery_manager`
   (`hermes_cli/plugins.py:990-999`) only scans when `_discovered` is false, and
   `PluginManager.discover_and_load` returns early once its flag is set
   (`hermes_cli/plugins.py:408`). There is no re-scan for a long-lived process.
   The only production fire site for `on_session_end` is
   `agent/turn_finalizer.py:911-922`. A chat, gateway or `serve` process that
   booted *before* the plugin was installed therefore dispatches every turn into
   an empty registry — and delegated subagents are constructed in that same
   process (`tools/delegate_tool.py:303`), so they inherit the empty registry
   too. Cron is a new process per run, so it was the only surface that loaded
   the plugin. This half is operational: the plugin must be installed and the
   surface restarted (or the surface must start after install).
2. **The scope allowlist can never match a real chat turn.** A genuine
   interactive turn carries the task id the runtime minted for it
   (`agent/turn_context.py:643`: `task_id or str(uuid.uuid4())`), and
   `_in_scope` admitted only `task_id_prefix_allow` prefixes. A uuid matches no
   prefix, so the turn was `SKIPPED_OUT_OF_SCOPE` before a single journal row —
   `require_task_id: false` does not help, because the id is present, just
   unrecognisable.

The admission is one explicit, session-scoped key:

```yaml
scope:
  require_task_id: true
  task_id_prefix_allow: ["cron:", "subagent-"]
  task_id_exclude_prefixes: ["cron:8cff233194f6", "local-"]
  interactive_sessions: ["<the chat session id that may wake the lane>"]
```

A completed turn whose `session_id` is listed is in scope whatever shape its
minted task id has. Absent, empty or malformed it widens nothing (default deny
stands), and `task_id_exclude_prefixes` still wins.

Delegated subagents need no scope change: `tools/delegate_tool_child_run.py:885`
mints `subagent-<n>-<hex>` and passes it as `task_id` (`:1019`), which the
existing allowlist already admits — they were silent only because of cause 1.

**Evidence:** RED `PYTEST_EXIT=1` (2 failed: interactive admission + fresh-process
dispatch) — `RED_W1F.txt`; GREEN `PYTEST_EXIT=0` (14 passed) — `GREEN_W1F.txt`;
full suite `PYTEST_EXIT=0` (171 passed = 157 existing + 14 new) —
`GREEN_FULL_W1F.txt`.
