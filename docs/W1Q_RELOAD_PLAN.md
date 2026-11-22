# W1q — Non-cron wake trigger: reload plan (DOCUMENT ONLY)

**Status: NOT EXECUTED.** Nothing was installed, reloaded, restarted, deployed,
set live config, touched cron, sent a message, or contacted any seat. All proof
came from isolated git worktrees, throwaway `HERMES_HOME` fixtures and a stub
transport. **No process on this host was restarted or signalled by me.**

**Review target: the final published SHA** (code rebase `3182b08` + evidence
commit). `0f467fd` is superseded input only; it predates the W1i/W1j ACK work.

---

## 1. Which process serves interactive turns and delegated subagents

Read-only `psutil` census, saved as `PROCESS_CENSUS_W1Q.txt`:

| PID | Start | Command line (trimmed) | Role |
|---|---|---|---|
| **42332** | 2026-11-20 18:51:45 | `python.exe -m hermes_cli.main --profile default gateway run --quiet` | **gateway**. Fires cron events; already force-reloaded at install. |
| **12312 → 23600 → 23092 → 18936** | 2026-11-18 22:56:23/24 | `hermes.exe serve --skip-build` and its python children | **chat / TUI surface** — hosts sessions, finalizes interactive turns and constructs delegated subagents. This is the process that never loaded the plugin. |
| 7856 | 2026-11-18 22:53:46 | `Hermes.exe` (Electron) | Desktop UI shell around the surface above. |

The surface is therefore a **different process from the gateway**. Evidence that
the `serve` process is the chat host: `hermes_cli/plugins_activation.py:238`
(`tui_gateway.server` loaded in a process == "this process hosts chats"), and
`tests/tui_gateway/test_plugins_manage_late_activation.py:46` ("the serve process
booted long before the install" then drives `tui_gateway.server.handle_request`).

---

## 2. Critical question: can the chat surface be hot-reloaded? **YES.**

A restart is **sufficient but not necessary** — for the gateway *and* for the
chat surface. The complete force-reload chain was read in code:

1. `tui_gateway/server.py:193` allows the `plugins.manage` RPC; handler at
   `tui_gateway/methods_tools.py:1937`, action map at `:1930-1934` includes
   `toggle`.
2. `_plugins_toggle` (`tui_gateway/methods_tools.py:1741-1758`) calls
   `hermes_cli.plugins_cmd.dashboard_set_agent_plugin_enabled`.
3. `hermes_cli/plugins_cmd.py:1032-1061`: on an actual enable it calls
   `activate_plugin_now(key)` (`:1053-1057`).
4. `hermes_cli/plugins_activation.py:128-144`: `activate_plugin_now(...,
   in_process=True)` calls `load_and_go_live(name)`. Its docstring says it loads
   the plugin **in THIS process so `on_plugin_loaded` subscribers here — the
   TUI/Desktop server — see it**.
5. `hermes_cli/plugins_activation.py:215-224`: `load_and_go_live` calls
   `manager.discover_and_load(force=True)` — the forced rescan that a plain
   non-forced call cannot do (`plugins.py:402-409` short-circuits on
   `_discovered`).
6. `:147` then calls `reload_running_gateway()` → gateway control-socket
   `reload-plugins` (`gateway/control_socket.py:588-596`, verb registered at
   `gateway/run_bootstrap.py:374`, force rescan at
   `gateway/run_plugin_rewire.py:82-110`). **One toggle reloads both processes.**

Alternative, no config toggle needed: `POST /api/dashboard/agent-plugins/activate`
(`hermes_cli/web_routers/dashboard_ui.py:308-332`) calls `load_and_go_live(name)`
directly in the backend process; token required.

Non-paths, checked: gateway SIGUSR1 restart handler (`gateway/run.py:4743`,
`gateway/run_bootstrap.py:664-671`) is POSIX-only and is a restart, not a reload;
chat-surface signal handlers (`cli_tui_runtime_mixin.py:471-482`) are shutdown /
disconnect only; `main_dashboard.py:735` force rescan runs in the CLI auth-setup
process, not the live surface.

**Answer: the chat surface is NOT the component that cannot be hot-reloaded.**
A live proof needs neither a restart nor a fresh process-with-plugin-at-boot,
provided the owner authorises the trigger below.

---

## 3. Primary route — force reload (no restart)

Exact TUI RPC trigger, issued by the operator inside the authenticated chat
surface (no agent send, no contact with any seat):

```json
{"id":"w1q-reload","method":"plugins.manage",
 "params":{"action":"toggle","key":"wake-emitter","enable":true}}
```

If the plugin is already enabled (so the toggle reports `unchanged: true`, which
does **not** activate — `plugins_cmd.py:1053`), use the dashboard activate
endpoint instead: `POST /api/dashboard/agent-plugins/activate` with
`{"name":"wake-emitter","home":"C:/Users/wigmore/.hermes"}` and the backend
token supplied by the operator.

What this does / does not reload:
- **does**: force re-discovers all plugins in the chat-surface process, registers
  `on_session_end` there, connects plugin MCP servers, updates open chats
  (`refresh_plugin_sessions`), and force re-discovers in the gateway via the
  control socket;
- **does not**: switch Python dependency generations — `restart_required` stays
  true when a dependency cannot be imported in a running process
  (`plugins_activation.py:136-142`);
- **does not**: un-wire handlers a disabled plugin already attached — disable is
  config-only until restart (`plugins_cmd.py:1064-1066`).

Fallback only: restart the chat surface (PIDs 12312/23600/23092/18936 and the
Electron shell PID 7856) via its normal launcher, operator-initiated. This drops
open conversations and unsent turns; it is the last resort, not the plan.

## 4. Blast radius

Force re-discovery calls `unload()` before reloading
(`hermes_cli/plugins.py:413-414`), so in each affected process, briefly:
model/web providers, plugin skills, MCP servers and config-owned shell hooks /
outbound webhooks are absent and re-register (`plugins.py:433-438`); turns
finalized during the pass dispatch into an empty registry (the current broken
state); open chats may lose tools for the length of the pass. Cron events keep
firing from PID 42332 apart from the short gateway pass.

## 5. Rollback if the plugin misbehaves

1. Brake: `plugins.manage` `toggle` with `enable:false` (config-only — a running
   gateway keeps already-wired handlers until restart; expect that caveat).
2. Byte-exact config + plugin restore (staging repo script W1g-hardened):

```bash
env -u PYTHONPATH HERMES_DISABLE_LAZY_INSTALLS=1 \
  C:/Users/wigmore/.hermes/hermes-agent/venv/Scripts/python.exe \
  C:/Users/wigmore/oc-runs/w1q-critical/scripts/wake_activate.py \
  --home C:/Users/wigmore/.hermes --rollback \
  --expected-config-sha <SHA recorded at apply> --verify
```

It verifies the backup hash before overwriting, moves the plugin dir aside and
reports `verified/config_hash_matches/plugin_dir_absent`. Do not hand-edit config.

## 6. Verify success in under one minute

After one genuine interactive turn completes:

```bash
tail -n 8 C:/Users/wigmore/.hermes/state/event_outbox.jsonl
```

Success = `kind=event` with a **uuid** `task_id` (minted by the turn, e.g.
`e233f386-...`), not `cron:`; then `kind=attempt` (`ATTEMPTED`), then `kind=outcome`
settled `DELIVERED_ACKED` or `ACKED_ORDER_ONLY`; no `SKIPPED_OUT_OF_SCOPE`.

**Proof boundary:** stub transport proves hook dispatch, journal writes and
settlement logic. It does **not** prove real delivery to the GPT or a peer
acknowledgement. Both are **UNVERIFIED** until the live reload and a real turn.

## 7. Ordering constraint — config decision FIRST

The live config `C:\Users\wigmore\.hermes\config.yaml` is corrupted and any
reload path loads it. Read-only inspection showed `wake-emitter.settings.enabled:
false` (line 890), the wrong key `peer_lane_map_path` (line 893; the emitter
reads `peer_map_path`), no `scope.interactive_sessions`, and the documented
26-boolean YAML-round-trip corruption
(`docs/superpowers/plans/2026-11-22-w1g-byte-exact-rollback.md:5`). Note also
that `plugins.manage toggle` writes config via `save_config` (`plugins_cmd.py:
1028-1029`), so the owner's config decision must precede the toggle, and the
written bytes must be checked. **Config decision first, reload second.**

## 8. Abort criteria

Abort and roll back if: config guard reports `DRIFT_DETECTED` /
`POST_VERIFY_MISMATCH`; the toggle reports `restart_required: true` for a
missing dependency; plugin load errors or hook-budget overruns appear; a journal
row shows a `cron:` task_id for an interactive turn, a duplicate send, or no
terminal settlement; provider/skill/MCP registries do not recover; or real
delivery / peer ACK cannot be observed — do not claim end-to-end wake then.
