# W1g Byte-Exact Rollback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the wake installer/rollback byte-exact and crash-safe — byte copy instead of YAML round-trip, measured before/after hashes, three-way plugin state consistency, four injected faults, and a dry-run diff control that would have caught today's 26-boolean corruption.

**Architecture:** Modify `scripts/wake_activate.py` to: (1) preserve config bytes during apply using ruamel.yaml round-trip properly, (2) record truthful before/after config hashes at mutation boundaries, (3) enforce three-way plugin state consistency on rollback with pre-flight checks, (4) add fault injection tests for four crash scenarios, (5) add a dry-run mode that shows intended hashes and diff line count.

**Tech Stack:** Python 3.11, pytest, ruamel.yaml, stdlib (hashlib, shutil, tempfile, os, json, subprocess)

**Spec:** This plan implements the W1g defect statement requirements 1-5.

## Global Constraints

- STAGING ONLY: never run installer against live profile, never edit ~/.hermes/config.yaml, no restart, no deploy, no cron change
- All testing against COPIES of config fixtures in temp dirs only
- Tests RED first with real pytest exit codes against current code, then GREEN
- Report: defect line, diff of fix, RED/GREEN counts with exit codes, commit SHA, fault injection table
- No sends, no agent contact, no network calls

## Review Focus

1. **YAML round-trip corruption** — any boolean not explicitly set gets written as schema default (false)
2. **Hash measurement timing** — before/after hashes must be measured at mutation boundaries, not carried from earlier phase
3. **Three-way plugin state** — enabled list, settings.enabled, and plugin directory must be coherent after rollback
4. **Partial failure states** — crash between temp write and rename, permission failure, concurrent writer, dir move vs config write mismatch
5. **Dry-run observability** — operator must see exact before/after hashes and diff line count before authorizing

---

### Task 1: Fix save_config to preserve bytes (ruamel.yaml round-trip) and add dry-run diff mode

**Files:**
- Modify: `scripts/wake_activate.py:91-109` (save_config)
- Modify: `scripts/wake_activate.py:770-827` (main, add --dry-run/--verify mode)
- Test: `tests/test_w1g_byte_exact.py` (new file)

**Interfaces:**
- Consumes: `load_config` (uses ruamel.yaml rt), `enable_in_config` (modifies data dict)
- Produces: `save_config` that preserves comments/ordering/booleans; new `dry_run_diff` function

- [ ] **Step 1.1: Write failing test for byte-level restore (Requirement 1)**

```python
def test_byte_level_restore_sha256_equality(tmp_path):
    """Post-rollback bytes IDENTICAL to backup (sha256 equality).
    Fixture: config with booleans true/false, comments, ordering, unusual key."""
```

Run: `pytest tests/test_w1g_byte_exact.py::test_byte_level_restore_sha256_equality -v`
Expected: FAIL (current save_config uses yaml.safe_dump which loses comments/ordering)

- [ ] **Step 1.2: Write failing test for dry-run diff mode (Requirement 5)**

```python
def test_dry_run_shows_before_after_hashes_and_diff_count(tmp_path):
    """Dry-run prints intended before/after hashes and diff line count."""
```

Run: `pytest tests/test_w1g_byte_exact.py::test_dry_run_shows_before_after_hashes_and_diff_count -v`
Expected: FAIL (no dry-run mode exists)

- [ ] **Step 1.3: Fix save_config to use ruamel.yaml round-trip properly**

The current code already tries to use ruamel.yaml but the data dict may carry schema defaults.
Fix: Ensure `enable_in_config` only sets explicit keys, never relies on schema defaults.
Use ruamel.yaml's `CommentedMap`/`CommentedSeq` to preserve structure.

- [ ] **Step 1.4: Implement dry-run/verify mode**

Add `--verify` flag that:
- Loads config, simulates enable_in_config
- Computes before_hash = sha256(config_file)
- Computes after_hash = sha256(serialized_modified_config)
- Runs diff -u and counts changed lines
- Prints all three, exits 0 (no writes)

- [ ] **Step 1.5: Run tests to verify GREEN**

Run: `pytest tests/test_w1g_byte_exact.py -v`
Expected: PASS

- [ ] **Step 1.6: Commit**

```bash
git add scripts/wake_activate.py tests/test_w1g_byte_exact.py
git commit -m "W1g: byte-level restore with ruamel round-trip + dry-run diff mode"
```

---

### Task 2: Record truthfulness — measure hashes at mutation boundaries

**Files:**
- Modify: `scripts/wake_activate.py:499-635` (apply function)
- Modify: `scripts/wake_activate.py:638-708` (rollback function)
- Test: `tests/test_w1g_byte_exact.py` (add tests)

**Interfaces:**
- Consumes: `sha256` function, `config_before` measured at line 509
- Produces: `config_sha256_before` in PRE record, `config_sha256_after` in APPLIED record, both measured at mutation boundaries

- [ ] **Step 2.1: Write failing test for three distinct hashes when config edited between phases**

```python
def test_three_distinct_hashes_when_config_edited_between_phases(tmp_path):
    """Config edited between pre-measure, mutation, post-measure produces three distinct hashes."""
```

Run: `pytest tests/test_w1g_byte_exact.py::test_three_distinct_hashes_when_config_edited_between_phases -v`
Expected: FAIL (current code doesn't measure after hash)

- [ ] **Step 2.2: Write failing test for before != after when rollback genuinely changes bytes**

```python
def test_rollback_changes_bytes_before_ne_after_hash(tmp_path):
    """Rollback that genuinely changes bytes shows before != after."""
```

Run: `pytest tests/test_w1g_byte_exact.py::test_rollback_changes_bytes_before_ne_after_hash -v`
Expected: FAIL (rollback doesn't record after hash in record)

- [ ] **Step 2.3: Fix apply to measure config_sha256_after immediately after save_config**

Add after line 603: `config_after = sha256(paths.config)` and record it.

- [ ] **Step 2.4: Fix rollback to measure config_sha256_after immediately after restore**

Add after line 678: `config_after = sha256(paths.config)` and record it in rollback section.

- [ ] **Step 2.5: Ensure hashes are measured by hashing the file, not carried from variables**

Verify `sha256(paths.config)` is called at the exact mutation boundary, not using a cached value.

- [ ] **Step 2.6: Run tests to verify GREEN**

Run: `pytest tests/test_w1g_byte_exact.py -v`
Expected: PASS

- [ ] **Step 2.7: Commit**

```bash
git add scripts/wake_activate.py tests/test_w1g_byte_exact.py
git commit -m "W1g: record truthfulness - measure hashes at mutation boundaries"
```

---

### Task 3: Three-way plugin state consistency

**Files:**
- Modify: `scripts/wake_activate.py:638-708` (rollback function)
- Add: pre-flight consistency check function
- Test: `tests/test_w1g_byte_exact.py` (add tests)

**Interfaces:**
- Consumes: `read_flag` (enabled list, settings.enabled), `paths.dst.exists()` (plugin dir)
- Produces: invariant enforcement, pre-flight check that refuses on inconsistent state

- [ ] **Step 3.1: Write failing test for three-way coherence after rollback**

```python
def test_three_way_coherence_after_rollback(tmp_path):
    """After rollback: wake-emitter removed from enabled list AND settings.enabled=false AND plugin dir moved aside."""
```

Run: `pytest tests/test_w1g_byte_exact.py::test_three_way_coherence_after_rollback -v`
Expected: FAIL (current rollback only moves plugin dir, doesn't clean config)

- [ ] **Step 3.2: Write failing test for pre-flight check on already-inconsistent state**

```python
def test_preflight_refuses_on_inconsistent_state_reports_which_disagrees(tmp_path):
    """Pre-flight check refuses to proceed on inconsistent state, reports which of three disagrees."""
```

Run: `pytest tests/test_w1g_byte_exact.py::test_preflight_refuses_on_inconsistent_state_reports_which_disagrees -v`
Expected: FAIL (no pre-flight check exists)

- [ ] **Step 3.3: Define and enforce the invariant**

Invariant for rollback: plugin removed from enabled list AND settings.enabled=false AND plugin directory moved aside (or deleted).
Add `check_plugin_state_consistency(paths)` function that reads all three and asserts coherence.
Call it at start of rollout and rollback.

- [ ] **Step 3.4: Fix rollback to clean config (remove from enabled list, set settings.enabled=false)**

Currently rollback only restores config from backup (which is correct if backup is pre-apply).
But if backup is missing or corrupted, need explicit cleanup.
Also add pre-flight check before any operation.

- [ ] **Step 3.5: Run tests to verify GREEN**

Run: `pytest tests/test_w1g_byte_exact.py -v`
Expected: PASS

- [ ] **Step 3.6: Commit**

```bash
git add scripts/wake_activate.py tests/test_w1g_byte_exact.py
git commit -m "W1g: three-way plugin state consistency with pre-flight checks"
```

---

### Task 4: Fault injection tests (four scenarios)

**Files:**
- Modify: `scripts/wake_activate.py` (add rehearsal-only crash injection points)
- Test: `tests/test_w1g_fault_injection.py` (new file)

**Interfaces:**
- Consumes: `WAKE_STOP_AFTER` env var for crash injection, `WAKE_DRIFT` for concurrent writer
- Produces: four test cases with observed post-fault state

- [ ] **Step 4.1: Write failing test for fault (a) - crash between temp write and rename**

```python
def test_fault_a_crash_between_temp_write_and_rename(tmp_path):
    """Target remains byte-identical to pre-state, no partial file left behind."""
```

Run: `pytest tests/test_w1g_fault_injection.py::test_fault_a_crash_between_temp_write_and_rename -v`
Expected: FAIL (crash injection exists but test doesn't verify post-state)

- [ ] **Step 4.2: Write failing test for fault (b) - permission failure mid-restore**

```python
def test_fault_b_permission_failure_mid_restore(tmp_path):
    """Target unchanged, error named, no half-written config."""
```

Run: `pytest tests/test_w1g_fault_injection.py::test_fault_b_permission_failure_mid_restore -v`
Expected: FAIL

- [ ] **Step 4.3: Write failing test for fault (c) - concurrent writer**

```python
def test_fault_c_concurrent_writer_refuses(tmp_path):
    """Installer refuses rather than clobber config that changed since before-hash."""
```

Run: `pytest tests/test_w1g_fault_injection.py::test_fault_c_concurrent_writer_refuses -v`
Expected: FAIL (concurrent writer check exists at line 596 but test doesn't verify)

- [ ] **Step 4.4: Write failing test for fault (d) - plugin dir move succeeds but config write fails**

```python
def test_fault_d_plugin_dir_moved_config_write_fails(tmp_path):
    """No state where directory is gone and enabled list still says enabled."""
```

Run: `pytest tests/test_w1g_fault_injection.py::test_fault_d_plugin_dir_moved_config_write_fails -v`
Expected: FAIL

- [ ] **Step 4.5: Ensure rehearsal infrastructure supports all four faults**

The existing `_stop_after` and `_inject_drift` functions provide crash/drift injection.
Verify they cover all four scenarios; add injection points if needed.

- [ ] **Step 4.6: Run tests to verify GREEN**

Run: `pytest tests/test_w1g_fault_injection.py -v`
Expected: PASS

- [ ] **Step 4.7: Commit**

```bash
git add scripts/wake_activate.py tests/test_w1g_fault_injection.py
git commit -m "W1g: four fault injection tests with observed post-fault states"
```

---

### Task 5: Integration test suite and final verification

**Files:**
- Test: `tests/test_w1g_integration.py` (new file)
- Modify: existing tests if needed

**Interfaces:**
- Consumes: all previous fixes
- Produces: end-to-end verification, RED/GREEN counts table

- [ ] **Step 5.1: Write integration test that reproduces original 26-boolean corruption and verifies fix**

```python
def test_original_corruption_scenario_fixed(tmp_path):
    """Fixture with 26 booleans true/false, comments, ordering - dry-run shows 0 diff, rollback restores byte-exact."""
```

- [ ] **Step 5.2: Run full test suite**

Run: `pytest tests/test_w1g_*.py -v`
Collect RED/GREEN counts with exit codes.

- [ ] **Step 5.3: Generate fault injection table**

Create table with four faults, observed post-fault state of config bytes and plugin directory.

- [ ] **Step 5.4: Final commit with report**

```bash
git add tests/
git commit -m "W1g: integration test + fault injection table + final verification"
```

---

## Self-Review Checklist

- [ ] Spec coverage: All 5 requirements mapped to tasks
- [ ] Step granularity: Each step is one action with checkable result
- [ ] Type consistency: Function signatures match across tasks
- [ ] Review Focus: Each failure mode has a test in the owning task
- [ ] Proportion: Plan length appropriate for spec (not a transcript)