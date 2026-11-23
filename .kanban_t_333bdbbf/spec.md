# Spec: t_333bdbbf — completion must prove its receipt (plus the silent-drain)

## Measured defect (2026-11-15)
Card t_2464ccfe created with issuer_state=RESOLVED. `hermes kanban complete` returned rc 0;
delivery_obligations gained 0 rows, messages gained 0 rows, and
state/undelivered_envelopes.jsonl gained 0 ORIGIN-RECEIPT records. Completion reported
success while emitting nothing. Second defect: reoffer_tick.py runs every ~5 min, reports
last_status=ok, while the ledger holds ~931 envelopes at attempts:0 and the stall watch
reports undelivered=153 exhausted=28 — a drain that says ok while the backlog does not move.

## Scope (do NOT touch)
- The delivered-record INSERT (delivery_obligations/messages).
- Lane caps.
- Issuer resolution logic (separate card).
- The bootstrap / the wake supervisor gate (plugins/b4-turn-gate is inert).

## Files to change
1. hermes_cli/kanban_db.py — `complete_task`, ORIGIN RECEIPT region (~L2915-2935 bare
   append; L3017-3212 resolution + event). Add a receipt assertion that fails non-zero.
2. scripts/reoffer_tick.py — progress reporting + exit non-zero when undelivered count does
   not fall across runs. (bounded retry/backoff is already in undelivered.py; do not touch it.)

## Change A — complete_task receipt assertion
- Emit EXACTLY ONE receipt per completion: a ledger row in state/undelivered_envelopes.jsonl
  named ORIGIN-RECEIPT carrying task_id + issuer (so it greps: `grep -c 'task_id' ...`),
  and an ORIGIN_RECEIPT event in task_events.
- Asserts that at least one receipt record was written. If 0 receipts were written, raise
  `kb.ReceiptNotWrittenError(task_id, reason)` so complete_task exits non-zero with the reason
  (the CLI already maps IssuerUnresolvedError to a non-zero exit + message).
- Keep existing exception semantics for unroutable issuers (IssuerUnresolvedError) — those are
  the "deliberately unroutable completion" case that must exit non-zero naming the reason.
- Do not change the ledger row schema/fields (the INSERT) and do not change issuer resolution.

## Change B — reoffer_tick.py
- Print one line per real delivery, a counts line for every non-landed outcome (attempted /
  delivered / failed), and NO_DUE_RECORDS when nothing was due. Never print the whole JSON dump.
- Track ledger delta across runs (delivered + failed counts it changed).
- Exit non-zero when the undelivered count does not fall across runs, so the every-5m cron
  cannot keep reporting ok while the backlog is static.
- Reuse the bounded pass already in undelivered.py (MAX_REOFFERS, MAX_DEFERRALS, backoff);
  do not add a new daemon.

## Acceptance (a)-(d)
(a) Complete a task with an issuer; grep state/undelivered_envelopes.jsonl for the ORIGIN-RECEIPT
    line and show task_id + issuer are present on one line.
(b) Complete a task whose issuer cannot be routed (e.g. issuer not in seat index, or NULL).
    Show rc != 0 and the reason named (NotFoundError/IssuerUnresolvedError/ReceiptNotWrittenError).
(c) Run reoffer_tick.py twice consecutively; show undelivered count falls (from the shipped
    ~931 state to fewer), not stable.
(d) Red test proving the old silent-success path fails the new assertion:
    - complete a task without a routable issuer; assert `hermes kanban complete ...` returns
      non-zero / raises ReceiptNotWrittenError (not rc 0).

## Commands to verify
- (a) `HERMES_HOME=<tmp> python test_origin_receipt.py` then grep the envelope file.
- (b) unroutable issuer task; echo $? (expect >0) and read the failure message.
- (c) run scripts/reoffer_tick.py twice; count undelivered rows between runs.
- (d) pytest that asserts a no-receipt completion raises.
