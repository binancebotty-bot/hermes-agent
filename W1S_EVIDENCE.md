# W1s - one shipping tree (staging only)

## Inputs and provenance

| input | sha | contents |
|---|---|---|
| W1q tree | `c46a3712f8a886f31f732e9156146f70b672457e` | W1j ACK hardening (`cac8395`) + W1f interactive-scope admission (`3182b08`) + W1q evidence |
| W1r fix | `fc6d099f7627cd3625ccbbe5c03abca9f0a51b79` | judge only the reply half of a combined ACK block |

`git merge-base c46a371 fc6d099` = `cac8395` - the two branches share master and
diverged from it, so neither already contained the other.

Fresh worktree: `C:/Users/wigmore/oc-runs/w1s-shipping`, created with
`git worktree add <path> --detach c46a3712...`. Clean tree before work
(`git status --porcelain` empty). No other lane checkout was touched.

## Merge strategy and conflict resolution

**Cherry-pick, single patch:** `git cherry-pick fc6d099` onto the fresh
c46a371 worktree. Result: `064c83a`.

The two patches touch disjoint hunks of ONE file, `plugins/wake-emitter/emitter.py`:

- W1r rewrites which block `_is_our_outbound` judges (the ACK path, ~line 355);
- W1f adds the `interactive_sessions` key to `_in_scope` (the scope gate, ~line 712).

Git auto-merged with **no conflict markers**. That was verified, not trusted -
the composition was checked in both directions:

```
git diff c46a371 HEAD -- plugins/   -> exactly the W1r delta, nothing else
git diff fc6d099 HEAD -- plugins/   -> exactly the W1f delta, nothing else
```

So no third change entered the composition and neither side hunk was dropped.

## How the two ACK rules compose without weakening each other

1. **W1j five vetoes are untouched in order** - envelope markers
   (`ENVELOPE_MARKERS`), `SELF_ACK_PHRASE`, `SENDER_LINE_MARKERS`, and the
   recorded sent text. W1r changes only *which block* those vetoes are applied
   to, never which vetoes exist.
2. **The block is the reply half only when `REPLY_SPLIT` is present.** Without a
   separator `block is text`, so the strict whole-turn scan W1j depends on is
   still the behaviour for an unsplit block.
3. **A reply half that IS our payload is still rejected** - the same markers are
   scanned *inside* the reply half, so `payload+SEP+payload`,
   `payload+SEP+line 2` and `nonce+SEP+line 2` all stay `False`.
4. **W1f scope admission is untouched by the merge**: different function, not
   referenced from the ACK path, and its absent/empty/malformed key still
   widens nothing.

No semantic conflict was found, so nothing was chosen over anything.

## Reproducibility gap closed

`scripts/w1r_ack_edges_proof.py` existed only in the W1r worktree (`fc556d2`)
and was **not** in `fc6d099`, so the regression table could not be reproduced
from the commit that claims it. It is committed here and re-run on this tree:
15 variants, **0 mismatches, exit 0** (`PROOF_W1R_REGRESSION_MERGED.txt`).

## Proof results (all on the merged tree)

| # | proof | command | result |
|---|---|---|---|
| a | all five W1j guards | `pytest tests/test_w1j_ack_hardening.py -v` | **10 passed**, exit 0 (`W1S_A_W1J_GUARDS.txt`) |
| b | W1r self-ACK regressions | `pytest tests/test_w1r_ack_edges.py -v` | **8 passed**, exit 0 (`W1S_B_W1R_REGRESSIONS.txt`) |
| c | W1r edge cases now True | `scripts/w1r_ack_edges_proof.py` | **0 mismatches**, exit 0 (`PROOF_W1R_REGRESSION_MERGED.txt`) |
| d | W1f deny-safe truth table | `pytest tests/test_w1f_non_cron_wake.py -v` | **14 passed**, exit 0 (`W1S_D_W1F_TRUTH_TABLE.txt`) |
| e | fresh child process | `scripts/w1q_fresh_process_proof.py` | `W1Q_FRESH_GREEN=PASS`, exit 0 (`W1S_E_FRESH_CHILD.txt`) |
| - | full suite | `pytest tests -q` | **212 passed**, exit 0 |

(b) the five false variants stay `False`: our two-line payload, our one-line
payload, our line 2 alone, `payload+SEP+payload`, `payload+SEP+nothing`.

(e) the child process discovers the plugin, writes ONE event whose task id is the
runtime-minted uuid `22d8c021-ffc0-420d-930c-112e7781f2c6`, one `attempt`, and
settles `ACKED_ORDER_ONLY`; `grep -c cron` over the whole proof output = **0**.

## Mutation control (full table: `MUTATION_W1S_MERGED.txt`)

One revert at a time, **full suite** each time, byte-exact green source restored
after every run (sha256 re-asserted). Green emitter sha256:
`9a218eb1a2a7151b81f174158f27039436fab9d51f9bbe3643ff162407c31048`
(this is the MERGED emitter; W1r own `78d38a18...` is `fc6d099`, which has no
W1f delta).

| mutation | reds | pre-existing | new |
|---|---|---|---|
| M1 self-ACK rejection | 6 | 5 | 1 |
| M2 expiry ordering | 2 | 2 | 0 |
| M3 journal pairing | 1 | 1 | 0 |
| M4 status reconciliation | 1 | 1 | 0 |
| M5 clock guard (drift only) | 1 | 1 | 0 |
| M5b clock guard (drift + clamp = true pre-W1j body) | 2 | 2 | 0 |
| M6 session admission | 2 | 2 | 0 |
| M7 reply-half judging | 5 | 0 | 5 |

PRE-EXISTING = the test file exists at the merge base `c46a371`
(`git ls-tree c46a371 tests/`). NEW = `tests/test_w1r_ack_edges.py`, absent at
`c46a371`, added by this merge.

Verdict: M1-M6 each turn at least one PRE-EXISTING test red, so **no old guard is
protected only by a test added by this merge**. M7 is a NEW rule - nothing could
protect it before it existed - so its reds are correctly all NEW; that is stated,
not hidden. M5 as written leaves `test_5b` green because `max(0.0, ...)` alone
already absorbs a backward step; M5b (drift absorption and the clamp both
reverted) turns `5a` and `5b` red, matching the W1j record.

## Full suite from a `git archive` export (no .git, no worktree)

```
git archive fd2e900 | tar -x -C <scratch>/export-fd2e900
cd <scratch>/export-fd2e900
env -u PYTHONPATH HERMES_DISABLE_LAZY_INSTALLS=1 <venv>/python.exe -m pytest tests -q
```

`212 passed in 46.78s`, **REAL_EXIT=0** (`FULL_SUITE_W1S_EXPORT_A.txt`, captured
from the pytest process itself, not a trailing formatter).

## Tree identity across the two commits

`fd2e900` is the code commit; the evidence commit adds only `.md`/`.txt` files.
`git diff fd2e900 <evidence-commit> -- *.py` must be empty - the emitter blob
`5d74a908ec03f6d3b38296a3feb1f2f3442a1c4c` is the tested one.

## Staging only

No install, no live config write, no reload, no restart, no deploy, no cron
entry, no agent contact. Transports are stubs and every home directory is a temp
dir. UNVERIFIED: real delivery to a live GPT pane, real peer acknowledgement, and
behaviour under the live plugin config.
