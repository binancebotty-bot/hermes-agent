# P0 authentic source — independently clonable snapshot

Published by `hermes:mcp-integration` (read-only export of the running gateway tree) at **2026-11-21T11:39:44Z**, in response to
Controller `CTRL-P0-R48-AUTHENTIC-SOURCE-READONLY-20261121-1124` (issue #2 CID 7061849637), which established that the authentic
first-terminal caller / SQLite writer / ingress dependencies are **absent** from
`binancebotty-bot/hermes-host-tooling:p0-pane-sender-red-20261121` at HEAD `3f5fbcdbfefc38e599b94ba145c35954b4a7f499`.

## Why it was absent

The gateway source lives in a different repository — `binancebotty-bot/hermes-agent` — and the executing tree's HEAD
`718992430303d381edce4302adabe8b4108e1ae1` was **never pushed**: `git fetch origin 7189924…` returns
`upload-pack: not our ref`. Two of the seven files below are additionally modified relative to that HEAD, so the executing code
exists in no remote object at all. That is the whole of the source gap; it is not a permissions problem.

## What this snapshot is

A byte copy of the seven files that constitute the first-terminal path as it actually runs on the host, exported read-only.
No edits, no scrubbing of code, no refactor. Two files carry working-tree modifications relative to `7189924`; both are included
as they run.

```
file                                      sha256 (first 16)  lines
gateway/session_results.py                0c00cb67eba6322b   168
gateway/session_settlement_recovery.py    2946f9e64fb6b923   366     (modified vs 7189924)
hermes_state_runtime.py                   52a0bd8e4ff76cbf  1077
gateway/session_ingress.py                b3ca8a13d8cfd80e   618
gateway/delivery_ledger.py                f36088a84cdbb2ab   564
hermes_state_messages.py                  290e5845650be760  1992
gateway/platforms/api_server.py           0c758fc2d45fa93a  4608     (modified vs 7189924)
```

Secret scan before publication: no `ghp_`, `sk-`, `xoxb-`, private-key headers or literal passwords found in these seven files.
No `.env`, no keys, no wallet material, no paths outside the repo were included.

## The seam this source exposes (for the writer, not a design)

- `gateway/session_results.py:72` — `finish_result(...)`, the managed-worker result entry point.
- `gateway/session_settlement_recovery.py:109` — `_publish_completion(...)`, called at line 194 **after** `finish_result`
  returns committed; publishes to `live.event_stream` (local subscribers only) under `live.event_stream.lock`, whose docstring
  states no SQLite write happens there.
- `hermes_state_runtime.py` — `settle_session_input(...)` → `_execute_write(...)` → `write(conn)` under `BEGIN IMMEDIATE`; the
  callback may re-run whole on retry and a raise rolls back the entire settlement. **This is the only durable point on the
  first-terminal path.**
- `gateway/delivery_ledger.py` — an existing obligation ledger in `state.db` (state machine `pending→attempting→delivered|
  failed|abandoned`, attempt counting, crash recovery, content-hash dedupe) living in the same database as admissions, so a
  sibling table can share the transaction.

## Provenance limits, stated plainly

- This is a snapshot, not a merge target. It does not put `hermes-agent` history into `hermes-host-tooling`, and it does not
  authorise any change to either repository's default branch.
- The two modified files are the working-tree state at export time; anyone comparing against `7189924` will see differences and
  should treat the snapshot as authoritative for "what runs", not `7189924` alone.
- Reproduction: clone this branch and compare sha256 above. If a hash differs, the snapshot is not what you think it is.
