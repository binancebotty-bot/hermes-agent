# W1q — consolidated evidence (staging only)

## Provenance

- Code rebase: `git worktree add .../w1q-critical 0f467fd`, then `git rebase
  cac8395`. Single W1f commit replayed onto current master.
- Conflict: `README.md` only — additive (W1i section at HEAD vs W1f section
  incoming). Resolution: **kept both sections**, stripped conflict markers.
  `plugins/wake-emitter/emitter.py` auto-merged cleanly; no semantic conflict
  was found or invented. Final code SHA `3182b08`.
- Review target: the **final published SHA** (code rebase + this evidence
  commit). `0f467fd` is superseded input only.
- Graphify: no `graphify-out/` in this repo; CLI present but no basis to query.
  Git's own `show --stat` and source reads were used instead of tree-wide grep.

## Behaviour sets after the merge

Both ACK-hardening (W1j/W1h) and interactive-scope (W1f) paths pass together:

```
env -u PYTHONPATH HERMES_DISABLE_LAZY_INSTALLS=1 <venv>/python.exe \
  -m pytest tests/test_w1j_ack_hardening.py tests/test_w1h_ack_settlement.py \
            tests/test_w1f_non_cron_wake.py -q
PYTEST_EXIT=0
32 passed in 4.94s
```

Evidence file: `BOTH_SETS_W1Q.txt`.

## RED at unmodified HEAD `cac8395`

`tests/test_w1f_non_cron_wake.py` was copied unchanged into a fresh worktree at
`cac8395` (which has zero occurrences of `interactive_sessions`). The tests
assert the **correct** behaviour, not "zero rows":

- `test_listed_interactive_session_records_delivers_and_settles` expects
  `DELIVERED_UNACKED`, kinds `[event, attempt, outcome]`, one send, terminal ACK;
- `test_hook_dispatch_reaches_plugin_in_a_fresh_process` expects discovery,
  registration, one event with an interactive task id, one attempt and
  `ACKED_ORDER_ONLY`.

Result (`RED_W1Q_HEAD.txt`, worktree `C:/Users/wigmore/oc-runs/w1q-head-red`):

```
FAILED ...::test_listed_interactive_session_records_delivers_and_settles
  assert 'SKIPPED_OUT_OF_SCOPE' == 'DELIVERED_UNACKED'
FAILED ...::test_hook_dispatch_reaches_plugin_in_a_fresh_process
  {'hook_results': ['SKIPPED_OUT_OF_SCOPE'], 'sweep_result': ['SKIPPED'],
   'discovered': True, 'saw_plugin': True, 'rows': []}
  assert 0 == 1
2 failed, 12 passed
PYTEST_EXIT=1
```

This is RED for the right reason: the plugin loads and registers in a fresh
process, but the uuid task id of a genuine interactive turn is rejected by the
cron prefix allowlist before any row is written.

## GREEN in a fresh child process, no cron

`scripts/w1q_fresh_process_proof.py` (staging-only harness) uses a temp
`HERMES_HOME`, real `lifecycle.invoke_hook`, real plugin loader, stub sender and
stub pane readback. The reconciliation sweep uses a `subagent-` shaped id, so
there is no cron token in the child source or output.

```
CHILD_RC= 0
{"hook_results": ["DELIVERED_UNACKED"], "sweep_result": ["SKIPPED"],
 "discovered": true, "saw_plugin": true,
 "rows": [["event", "e233f386-5bad-40ef-b6ef-4c571fa0b624", null],
          ["attempt", null, "ATTEMPTED"],
          ["outcome", null, "DELIVERED_UNACKED"],
          ["outcome", null, "ACKED_ORDER_ONLY"]]}
[plugins] DEBUG Plugin wake-emitter registered hook: on_session_end
W1Q_FRESH_GREEN=PASS
UNVERIFIED=real_delivery_to_GPT, peer_acknowledgement (stub sender + stub pane read)
PROOF_EXIT=0
```

Evidence: `GREEN_W1Q_FRESH.txt`. Chain observed: discovery → registration →
hook → event row with a minted uuid task_id → delivery attempt → settlement.

**Proof boundary:** the stub proves hook dispatch, journal writes and settlement
logic. **Real delivery to the GPT and a peer acknowledgement are UNVERIFIED.**
No cron process, scheduler or event was involved at any point.

## Process / reload findings

See `PROCESS_CENSUS_W1Q.txt` and `docs/W1Q_RELOAD_PLAN.md`. Summary:
gateway PID 42332 (2026-11-20, `gateway run`) is separate from the chat surface
`hermes serve --skip-build` tree PIDs 12312/23600/23092/18936 (2026-11-18) plus
Electron PID 7856. The chat surface **can** be hot-reloaded: `plugins.manage`
`toggle` → `dashboard_set_agent_plugin_enabled` → `activate_plugin_now` →
`load_and_go_live` → `discover_and_load(force=True)` in the serving process, and
the same toggle nudges the gateway's `reload-plugins` control-socket verb. A
restart is sufficient but not necessary for both. The reload plan is a document
only; nothing was reloaded, restarted or signalled.

## Blocker for live proof

One dependency blocks live proof: **the corrupted live config**
`C:/Users/wigmore/.hermes/config.yaml` (`wake-emitter.enabled: false`, wrong
`peer_lane_map_path` key, no `interactive_sessions`, documented 26-boolean
round-trip corruption). Any reload — hot or restart — loads those bytes, and the
`plugins.manage toggle` path itself writes config. Config decision first, reload
second. Separately, real delivery/peer ACK cannot be proven with a stub; that
requires a live turn after the reload.
