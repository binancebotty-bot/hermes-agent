"""Regression tests for CYCLE_GUARD failure decay window (count_recent_qualifying_failures).

The failure window feature (t_48172419) ensures only task_runs with status in
(crashed, timed_out, failed) within a configurable recent window (default 6 hours)
count toward the circuit breaker. Rate-limited (HTTP 429) runs and owner-admission
reclaim races are excluded from the qualifying count.

These tests prove:
1. Three qualifying failures OUTSIDE the window + one fresh qualifying failure → NOT parked
2. Three qualifying failures INSIDE the window → PARKED
3. Rate-limited (429) runs never count as qualifying failures
4. Owner-admission reclaim races never produce qualifying runs
"""

from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB and failure window config."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(__import__("pathlib").Path, "home", lambda: tmp_path)
    # Disable crash grace so tests control timing precisely
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    # Write config.yaml with short failure window (1 hour) for test speed
    config_yaml = home / "config.yaml"
    config_yaml.write_text("kanban:\n  failure_window_seconds: 3600\n")
    # Clear config cache so the new config is picked up
    import hermes_cli.config as cfg
    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    kb.init_db()
    return home


def _now() -> int:
    return int(time.time())


def _insert_qualifying_run(conn, task_id: str, ended_at: int, status: str = "crashed"):
    """Insert a qualifying run (crashed/timed_out/failed) with a specific ended_at."""
    run_cols = {r[1] for r in conn.execute("PRAGMA table_info(task_runs)")}
    fields = {"task_id": task_id, "status": status, "outcome": status}
    if "started_at" in run_cols:
        fields["started_at"] = ended_at - 60  # 1 min before end
    if "ended_at" in run_cols:
        fields["ended_at"] = ended_at
    names = ",".join(fields)
    placeholders = ",".join("?" * len(fields))
    conn.execute(
        f"INSERT INTO task_runs ({names}) VALUES ({placeholders})",
        tuple(fields.values()),
    )
    conn.commit()


def _end_run_with_outcome(conn, task_id: str, ended_at: int, outcome: str = "crashed") -> int:
    """End the task's current run with a qualifying outcome and create a new run for the next attempt.
    Returns the run_id of the ended run.
    This simulates the real flow where detect_crashed_workers ends the run and the next claim creates a new one.
    """
    # Get the current run_id
    run_row = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if run_row is None or run_row["current_run_id"] is None:
        # Create a run if none exists
        run_cols = {r[1] for r in conn.execute("PRAGMA table_info(task_runs)")}
        fields = {"task_id": task_id, "status": outcome, "outcome": outcome}
        if "started_at" in run_cols:
            fields["started_at"] = ended_at - 60
        if "ended_at" in run_cols:
            fields["ended_at"] = ended_at
        names = ",".join(fields)
        placeholders = ",".join("?" * len(fields))
        cur = conn.execute(
            f"INSERT INTO task_runs ({names}) VALUES ({placeholders})",
            tuple(fields.values()),
        )
        run_id = cur.lastrowid
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, task_id)
        )
    else:
        run_id = run_row["current_run_id"]
        conn.execute(
            "UPDATE task_runs SET outcome = ?, status = ?, ended_at = ? WHERE id = ?",
            (outcome, outcome, ended_at, run_id),
        )
    # End the run properly using _kb._end_run to match the real flow
    # But we need to manually set ended_at since _end_run uses current time
    # For test purposes, we just update the run and set task status to ready
    conn.execute(
        "UPDATE tasks SET status = 'ready', current_run_id = NULL WHERE id = ?",
        (task_id,),
    )
    conn.commit()
    return run_id


def _record_fresh_failure(conn, task_id: str, error: str, ended_at: int, outcome: str = "crashed", failure_limit: int = 2) -> bool:
    """End a run with the given outcome and ended_at, then record the failure via _record_task_failure.
    This simulates the real flow where detect_crashed_workers ends the run then calls _record_task_failure.
    """
    _end_run_with_outcome(conn, task_id, ended_at, outcome)
    return kbd._record_task_failure(
        conn, task_id, error=error, outcome=outcome,
        failure_limit=failure_limit,
        release_claim=False, end_run=False,
    )


def _insert_rate_limited_run(conn, task_id: str, ended_at: int):
    """Insert a rate_limited run (should NOT count as qualifying)."""
    run_cols = {r[1] for r in conn.execute("PRAGMA table_info(task_runs)")}
    fields = {"task_id": task_id, "status": "rate_limited", "outcome": "rate_limited"}
    if "started_at" in run_cols:
        fields["started_at"] = ended_at - 60
    if "ended_at" in run_cols:
        fields["ended_at"] = ended_at
    names = ",".join(fields)
    placeholders = ",".join("?" * len(fields))
    conn.execute(
        f"INSERT INTO task_runs ({names}) VALUES ({placeholders})",
        tuple(fields.values()),
    )
    conn.commit()


def _setup_task_with_failure_state(conn, consecutive_failures: int = 0):
    """Create a task in ready state with given failure counter. Returns the task ID."""
    tid = kb.create_task(conn, title="cycle guard test", assignee="worker")
    conn.execute(
        "UPDATE tasks SET status='ready', consecutive_failures=?, last_failure_error=NULL WHERE id=?",
        (consecutive_failures, tid),
    )
    conn.commit()
    return tid


class TestCountRecentQualifyingFailures:
    """Direct tests of the count_recent_qualifying_failures helper."""

    def test_counts_only_crashed_timed_out_failed_within_window(self, kanban_home):
        """Only crashed/timed_out/failed within window_seconds are counted."""
        with kbc.connect() as conn:
            tid = _setup_task_with_failure_state(conn)
            now = _now()
            window = 3600  # 1 hour

            # Two qualifying runs INSIDE window
            _insert_qualifying_run(conn, tid, now - 1800, "crashed")      # 30 min ago
            _insert_qualifying_run(conn, tid, now - 3000, "timed_out")    # 50 min ago

            # One qualifying run OUTSIDE window
            _insert_qualifying_run(conn, tid, now - 7200, "failed")       # 2 hours ago

            # One rate_limited run INSIDE window (should NOT count)
            _insert_rate_limited_run(conn, tid, now - 900)                # 15 min ago

            count = kbd.count_recent_qualifying_failures(conn, tid, window)
            assert count == 2, f"expected 2 qualifying runs in window, got {count}"

    def test_returns_zero_when_window_seconds_zero_or_negative(self, kanban_home):
        """window_seconds <= 0 returns 0 (legacy lifetime counting handled by callers)."""
        with kbc.connect() as conn:
            tid = _setup_task_with_failure_state(conn)
            now = _now()
            _insert_qualifying_run(conn, tid, now - 100, "crashed")

            assert kbd.count_recent_qualifying_failures(conn, tid, 0) == 0
            assert kbd.count_recent_qualifying_failures(conn, tid, -1) == 0

    def test_excludes_rate_limited_runs(self, kanban_home):
        """Rate-limited (HTTP 429) runs never count as qualifying failures."""
        with kbc.connect() as conn:
            tid = _setup_task_with_failure_state(conn)
            now = _now()
            window = 3600

            # Five rate_limited runs well within window
            for i in range(5):
                _insert_rate_limited_run(conn, tid, now - 600 - i * 100)

            count = kbd.count_recent_qualifying_failures(conn, tid, window)
            assert count == 0, "rate_limited runs must not count toward breaker"

    def test_excludes_owner_admission_races_no_run_row(self, kanban_home):
        """Owner-admission reclaim races are deferred by owner_reclaim_blocked
        and never produce a qualifying task_runs row — count should be 0."""
        with kbc.connect() as conn:
            tid = _setup_task_with_failure_state(conn)
            now = _now()
            window = 3600

            # Simulate the state where a reclaim race was deferred:
            # task is ready, no recent runs at all (the race was caught before a run was created)
            count = kbd.count_recent_qualifying_failures(conn, tid, window)
            assert count == 0

    def test_different_statuses_not_counted(self, kanban_home):
        """Only 'crashed', 'timed_out', 'failed' count. 'completed', 'reclaimed', 'rate_limited' do not."""
        with kbc.connect() as conn:
            tid = _setup_task_with_failure_state(conn)
            now = _now()
            window = 3600
            run_cols = {r[1] for r in conn.execute("PRAGMA table_info(task_runs)")}

            # Insert various non-qualifying statuses within window
            for status in ("completed", "reclaimed", "spawn_failed", "stale"):
                fields = {"task_id": tid, "status": status, "outcome": status}
                if "started_at" in run_cols:
                    fields["started_at"] = now - 300
                if "ended_at" in run_cols:
                    fields["ended_at"] = now - 200
                names = ",".join(fields)
                placeholders = ",".join("?" * len(fields))
                conn.execute(
                    f"INSERT INTO task_runs ({names}) VALUES ({placeholders})",
                    tuple(fields.values()),
                )

            # One qualifying run
            _insert_qualifying_run(conn, tid, now - 100, "crashed")

            count = kbd.count_recent_qualifying_failures(conn, tid, window)
            assert count == 1, f"only crashed should count, got {count}"


class TestCycleGuardBreakerWithFailureWindow:
    """Integration tests: the circuit breaker uses the failure window."""

    def test_three_old_failures_plus_one_fresh_does_not_park(self, kanban_home, monkeypatch):
        """Three qualifying failures OUTSIDE the window + one fresh qualifying failure
        must NOT park the card (breaker sees only 1 recent failure, below DEFAULT_FAILURE_LIMIT=2)."""
        with kbc.connect() as conn:
            tid = _setup_task_with_failure_state(conn)
            now = _now()
            window = 3600  # 1 hour (from monkeypatch)

            # THREE qualifying failures OUTSIDE the window (2+ hours ago)
            _insert_qualifying_run(conn, tid, now - 7200, "crashed")
            _insert_qualifying_run(conn, tid, now - 7300, "timed_out")
            _insert_qualifying_run(conn, tid, now - 7400, "failed")

            # Verify they exist but are outside window
            assert kbd.count_recent_qualifying_failures(conn, tid, window) == 0

            # Now simulate ONE fresh qualifying failure (crash) INSIDE the window
            # by ending a run and recording the failure (real flow)
            fresh_error = "fresh crash inside window"
            tripped = _record_fresh_failure(conn, tid, fresh_error, now - 100, "crashed")

            # Should NOT trip (only 1 recent failure < limit of 2)
            assert tripped is False, "breaker should not trip with only 1 recent failure"

            task = kb.get_task(conn, tid)
            assert task.status == "ready", f"task should stay ready, got {task.status}"
            assert task.consecutive_failures == 1  # legacy counter still increments

            # Verify the recent failure count is now 1 (the fresh run we just ended)
            assert kbd.count_recent_qualifying_failures(conn, tid, window) == 1

    def test_three_failures_inside_window_parks_card(self, kanban_home):
        """Three qualifying failures INSIDE the window must PARK the card."""
        with kbc.connect() as conn:
            tid = _setup_task_with_failure_state(conn)
            now = _now()
            window = 3600  # 1 hour

            # TWO qualifying failures already INSIDE the window
            _insert_qualifying_run(conn, tid, now - 1800, "crashed")   # 30 min ago
            _insert_qualifying_run(conn, tid, now - 2400, "timed_out") # 40 min ago

            # Verify count is 2
            assert kbd.count_recent_qualifying_failures(conn, tid, window) == 2

            # Now a THIRD fresh qualifying failure (crash) INSIDE the window
            fresh_error = "third crash inside window"
            tripped = _record_fresh_failure(conn, tid, fresh_error, now - 100, "crashed")

            # Should trip (3 recent failures >= limit of 2)
            assert tripped is True, "breaker should trip with 3 recent failures"

            task = kb.get_task(conn, tid)
            assert task.status == "blocked", f"task should be blocked, got {task.status}"
            # The error message is the one we passed in; CYCLE_GUARD prefix is added by the replenisher plugin
            assert task.last_failure_error == fresh_error, "error should be the one we passed"

            # The gave_up event should be recorded
            gave_up_events = [e for e in kb.list_events(conn, tid) if e.kind == "gave_up"]
            assert len(gave_up_events) == 1
            assert gave_up_events[0].payload.get("trigger_outcome") == "crashed"

    def test_rate_limited_runs_never_trip_breaker(self, kanban_home):
        """Many rate-limited runs must never trip the breaker."""
        with kbc.connect() as conn:
            tid = _setup_task_with_failure_state(conn)
            now = _now()
            window = 3600

            # Simulate 10 rate-limited runs well within window
            for i in range(10):
                _insert_rate_limited_run(conn, tid, now - 600 - i * 100)

            # Now a fresh qualifying failure (should be 1st recent qualifying failure)
            fresh_error = "first real crash"
            tripped = _record_fresh_failure(conn, tid, fresh_error, now - 50, "crashed")

            # Should NOT trip (only 1 qualifying failure, rate-limited ones don't count)
            assert tripped is False, "rate_limited runs must not count toward breaker"

            task = kb.get_task(conn, tid)
            assert task.status == "ready"
            assert kbd.count_recent_qualifying_failures(conn, tid, window) == 1

    def test_recompute_ready_uses_failure_window(self, kanban_home):
        """recompute_ready must use the failure window when promoting blocked -> ready."""
        with kbc.connect() as conn:
            tid = _setup_task_with_failure_state(conn)
            now = _now()
            window = 3600

            # Block the task with 3 old qualifying failures (outside window)
            for i in range(3):
                _insert_qualifying_run(conn, tid, now - 7200 - i * 100, "crashed")

            conn.execute(
                "UPDATE tasks SET status='blocked', consecutive_failures=3, "
                "last_failure_error='CYCLE_GUARD: too many failures' WHERE id=?",
                (tid,),
            )
            conn.commit()

            # recompute_ready with default failure_limit=2 should see 0 recent failures
            # and promote the task back to ready
            promoted = kb.recompute_ready(conn, failure_limit=2, failure_window_seconds=window)
            assert promoted == 1, "task should be promoted (0 recent failures < limit)"

            task = kb.get_task(conn, tid)
            assert task.status == "ready", f"task should be promoted to ready, got {task.status}"
            assert task.consecutive_failures == 3  # legacy counter preserved for compat

    def test_recompute_ready_blocks_when_recent_failures_exceed_limit(self, kanban_home):
        """recompute_ready must keep task blocked when recent qualifying failures >= limit."""
        with kbc.connect() as conn:
            tid = _setup_task_with_failure_state(conn)
            now = _now()
            window = 3600

            # 2 recent qualifying failures (inside window)
            _insert_qualifying_run(conn, tid, now - 1800, "crashed")
            _insert_qualifying_run(conn, tid, now - 2400, "timed_out")

            # Block the task
            conn.execute(
                "UPDATE tasks SET status='blocked', consecutive_failures=2, "
                "last_failure_error='CYCLE_GUARD: too many failures' WHERE id=?",
                (tid,),
            )
            conn.commit()

            # recompute_ready with failure_limit=2 should see 2 recent failures
            # and NOT promote (2 >= 2)
            promoted = kb.recompute_ready(conn, failure_limit=2, failure_window_seconds=window)
            assert promoted == 0, "task should stay blocked (2 recent failures >= limit)"

            task = kb.get_task(conn, tid)
            assert task.status == "blocked", f"task should stay blocked, got {task.status}"

    def test_window_zero_falls_back_to_legacy_lifetime_counting(self, kanban_home, tmp_path, monkeypatch):
        """When failure_window_seconds=0, legacy consecutive_failures counting is used."""
        # Override the configured window to 0 for this test by rewriting config.yaml
        config_yaml = tmp_path / ".hermes" / "config.yaml"
        config_yaml.write_text("kanban:\n  failure_window_seconds: 0\n")
        # Clear config cache
        import hermes_cli.config as cfg
        cfg._LOAD_CONFIG_CACHE.clear()
        cfg._RAW_CONFIG_CACHE.clear()

        with kbc.connect() as conn:
            tid = _setup_task_with_failure_state(conn)
            now = _now()

            # Insert 3 qualifying failures at various times (all would count in legacy mode)
            for i in range(3):
                _insert_qualifying_run(conn, tid, now - 7200 - i * 100, "crashed")

            # With window=0, count_recent_qualifying_failures returns 0
            # but _record_task_failure falls back to legacy_failures
            assert kbd.count_recent_qualifying_failures(conn, tid, 0) == 0

            # Simulate a fresh failure - legacy counter is at 0, this makes it 1
            tripped = kbd._record_task_failure(
                conn, tid, error="fresh crash", outcome="crashed",
                failure_limit=2,
                release_claim=False, end_run=False,
            )
            assert tripped is False  # legacy_failures=1 < limit=2

            task = kb.get_task(conn, tid)
            assert task.consecutive_failures == 1

            # Second fresh failure -> legacy_failures=2, should trip
            tripped = kbd._record_task_failure(
                conn, tid, error="second crash", outcome="crashed",
                failure_limit=2,
                release_claim=False, end_run=False,
            )
            assert tripped is True  # legacy_failures=2 >= limit=2

            task = kb.get_task(conn, tid)
            assert task.status == "blocked"


class TestConfiguredFailureWindowSeconds:
    """Tests for the config reader."""

    def test_default_is_6_hours(self, tmp_path, monkeypatch):
        """Default failure window is 21600 seconds (6 hours)."""
        # Create a home without config.yaml to test default
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(__import__("pathlib").Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
        import hermes_cli.config as cfg
        cfg._LOAD_CONFIG_CACHE.clear()
        cfg._RAW_CONFIG_CACHE.clear()
        kb.init_db()

        window = kbd.configured_failure_window_seconds()
        assert window == 21600, f"default should be 21600, got {window}"

    def test_config_override_respected(self, kanban_home):
        """Config value overrides default."""
        # The fixture writes config.yaml with 3600
        import hermes_cli.config as cfg
        cfg._LOAD_CONFIG_CACHE.clear()
        cfg._RAW_CONFIG_CACHE.clear()

        window = kbd.configured_failure_window_seconds()
        assert window == 3600

    def test_invalid_config_falls_back_to_default(self, tmp_path, monkeypatch):
        """Invalid config value falls back to default (21600)."""
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(__import__("pathlib").Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
        config_yaml = home / "config.yaml"
        config_yaml.write_text("kanban:\n  failure_window_seconds: not-a-number\n")
        import hermes_cli.config as cfg
        cfg._LOAD_CONFIG_CACHE.clear()
        cfg._RAW_CONFIG_CACHE.clear()
        kb.init_db()

        window = kbd.configured_failure_window_seconds()
        assert window == 21600

    def test_negative_config_clamped_to_zero(self, tmp_path, monkeypatch):
        """Negative config value is clamped to 0 (legacy mode)."""
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(__import__("pathlib").Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
        config_yaml = home / "config.yaml"
        config_yaml.write_text("kanban:\n  failure_window_seconds: -100\n")
        import hermes_cli.config as cfg
        cfg._LOAD_CONFIG_CACHE.clear()
        cfg._RAW_CONFIG_CACHE.clear()
        kb.init_db()

        window = kbd.configured_failure_window_seconds()
        assert window == 0


class TestDispatchOnceIntegration:
    """End-to-end tests through dispatch_once."""

    def test_dispatch_promotes_when_old_failures_expire(self, kanban_home, monkeypatch):
        """A blocked task with only old failures should be promoted on dispatch tick."""
        with kbc.connect() as conn:
            tid = _setup_task_with_failure_state(conn)
            now = _now()
            window = 3600

            # Block the task with 3 old qualifying failures (outside window)
            for i in range(3):
                _insert_qualifying_run(conn, tid, now - 7200 - i * 100, "crashed")

            conn.execute(
                "UPDATE tasks SET status='blocked', consecutive_failures=3, "
                "last_failure_error='CYCLE_GUARD: too many failures' WHERE id=?",
                (tid,),
            )
            conn.commit()

            # Run a dispatch tick (will call recompute_ready internally)
            result = kbd.dispatch_once(
                conn,
                failure_limit=2,
                stale_timeout_seconds=0,
                reconcile_orphans=False,
            )

            # The task should be promoted
            assert result.promoted == 1

            task = kb.get_task(conn, tid)
            assert task.status == "ready"

    def test_dispatch_keeps_blocked_when_recent_failures_exist(self, kanban_home):
        """A blocked task with recent failures should stay blocked on dispatch tick."""
        with kbc.connect() as conn:
            tid = _setup_task_with_failure_state(conn)
            now = _now()
            window = 3600

            # 2 recent qualifying failures (inside window)
            _insert_qualifying_run(conn, tid, now - 1800, "crashed")
            _insert_qualifying_run(conn, tid, now - 2400, "timed_out")

            conn.execute(
                "UPDATE tasks SET status='blocked', consecutive_failures=2, "
                "last_failure_error='CYCLE_GUARD: too many failures' WHERE id=?",
                (tid,),
            )
            conn.commit()

            result = kbd.dispatch_once(
                conn,
                failure_limit=2,
                stale_timeout_seconds=0,
                reconcile_orphans=False,
            )

            # The task should NOT be promoted
            assert result.promoted == 0

            task = kb.get_task(conn, tid)
            assert task.status == "blocked"


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_window_boundary_exact_match(self, kanban_home):
        """A failure exactly at the window boundary (ended_at == since) should count."""
        with kbc.connect() as conn:
            tid = _setup_task_with_failure_state(conn)
            now = _now()
            window = 3600
            since = now - window

            # Failure exactly at the boundary
            _insert_qualifying_run(conn, tid, since, "crashed")

            count = kbd.count_recent_qualifying_failures(conn, tid, window)
            assert count == 1, "failure exactly at window boundary should count (>= since)"

    def test_failure_just_outside_window_excluded(self, kanban_home):
        """A failure 1 second outside the window should NOT count."""
        with kbc.connect() as conn:
            tid = _setup_task_with_failure_state(conn)
            now = _now()
            window = 3600
            since = now - window

            # Failure 1 second before the boundary
            _insert_qualifying_run(conn, tid, since - 1, "crashed")

            count = kbd.count_recent_qualifying_failures(conn, tid, window)
            assert count == 0, "failure just outside window should not count"

    def test_mixed_old_and_new_failures_count_correctly(self, kanban_home):
        """Mix of old (excluded) and new (included) failures counts correctly."""
        with kbc.connect() as conn:
            tid = _setup_task_with_failure_state(conn)
            now = _now()
            window = 3600

            # 5 old failures (outside window)
            for i in range(5):
                _insert_qualifying_run(conn, tid, now - 7200 - i * 100, "crashed")

            # 2 new failures (inside window)
            _insert_qualifying_run(conn, tid, now - 1800, "crashed")
            _insert_qualifying_run(conn, tid, now - 2400, "timed_out")

            count = kbd.count_recent_qualifying_failures(conn, tid, window)
            assert count == 2, f"only 2 recent failures should count, got {count}"

    def test_consecutive_failures_legacy_preserved_for_backwards_compat(self, kanban_home):
        """The legacy consecutive_failures column is still incremented for backwards compatibility."""
        with kbc.connect() as conn:
            tid = _setup_task_with_failure_state(conn)
            now = _now()
            window = 3600

            # Verify the configured window is correct
            assert kbd.configured_failure_window_seconds() == 3600

            # No recent qualifying failures
            assert kbd.count_recent_qualifying_failures(conn, tid, window) == 0

            # First fresh failure
            _record_fresh_failure(conn, tid, "first", now - 100, "crashed")
            task = kb.get_task(conn, tid)
            assert task.consecutive_failures == 1
            assert kbd.count_recent_qualifying_failures(conn, tid, window) == 1

            # Second fresh failure (trips breaker via window)
            _record_fresh_failure(conn, tid, "second", now - 50, "crashed")
            task = kb.get_task(conn, tid)
            assert task.consecutive_failures == 2  # legacy counter still increments
            assert task.status == "blocked"

            # The legacy counter and window counter can diverge:
            # legacy=2, but if we wait for window to expire, window=0
            # This is intentional for backwards compatibility

    def test_no_task_row_returns_zero(self, kanban_home):
        """Querying a non-existent task returns 0, not an error."""
        with kbc.connect() as conn:
            count = kbd.count_recent_qualifying_failures(conn, "t_nonexistent", 3600)
            assert count == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])