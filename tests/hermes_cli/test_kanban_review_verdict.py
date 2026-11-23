"""Tests for the review-verdict loop closer (kanban_db.close_review_loop).

Covers the loop closer's RED/GREEN behaviour from the task body:

  * CHANGES_REQUIRED -> exactly one rework card, parented on the review task,
    carrying the blocker text verbatim, with the SAME free provider as the
    original fix.
  * the same review completion re-run -> no second card (idempotency key).
  * PASS -> no card, reviewer_verdict emitted, parent accepted, no card.
  * generation counter stops at 3; the 4th CHANGES_REQUIRED -> LOOP_EXHAUSTED.
  * BLOCKED -> reviewer_verdict with the reason, chain stopped.
"""

from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from hermes_cli import kanban_db_connect as kbc  # noqa: E402
from hermes_cli import kanban_db as kb  # noqa: E402
from hermes_cli.kanban_db import close_review_loop  # noqa: E402
from hermes_cli.kanban_db import resolve_issuer  # noqa: E402

REVIEW_SHAS = {
    "initial": "sha-1111-2222",
    "first": "sha-3333-4444",
    "second": "sha-5555-6666",
    "third": "sha-7777-8888",
    "fourth": "sha-9999-0000",
}

FREE_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
FREE_PROVIDER = "openrouter"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT",
                "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants  # type: ignore
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    return home


@pytest.fixture
def conn(fresh_home):
    c = kbc.connect()
    try:
        yield c
    finally:
        c.close()


def _create_fix(c, assignee="default"):
    return kb.create_task(
        c, title="FIX the thing", body="change this", assignee=assignee,
        provider_override=FREE_PROVIDER, model_override=FREE_MODEL,
        idempotency_key="fix-seed",
    )


def _create_review(c, fix_id, assignee="default"):
    return kb.create_task(
        c, title="REVIEW the fix", body="review this", assignee=assignee,
        parents=(fix_id,),
    )


def _fix_parent(c, fix_id):
    row = c.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ?", (fix_id,)
    ).fetchone()
    return row["parent_id"] if row else None


def _review_parent(c, review_id):
    """Return the parent of a review task (the fix task)."""
    row = c.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ?", (review_id,)
    ).fetchone()
    return row["parent_id"] if row else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _block_kind(c, task_id):
    row = c.execute(
        "SELECT block_kind FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return row["block_kind"] if row else None


def _reviewer_verdicts(c, task_id):
    out = []
    for ev in kb.list_events(c, task_id):
        if ev.kind == "reviewer_verdict":
            out.append(ev.payload)
    return out


# ---------------------------------------------------------------------------
# Test matrix
# ---------------------------------------------------------------------------

class TestChangesRequired:
    def test_creates_exactly_one_rework_card_parented_on_the_review(self, conn):
        fix = _create_fix(conn)
        review = _create_review(conn, fix)
        result = close_review_loop(
            conn, review, "CHANGES_REQUIRED", REVIEW_SHAS["initial"],
            blockers=["the pipeline drops the header", "memory leak in step 3"],
        )
        assert result["idempotent"] is False
        assert result["verdict"] == "CHANGES_REQUIRED"
        assert result["rework_card"]
        assert result["generation"] == 1

        rework = kb.get_task(conn, result["rework_card"])
        parent_rows = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?", (result["rework_card"],)
        ).fetchall()
        assert [r["parent_id"] for r in parent_rows] == [review]
        assert rework.provider_override == FREE_PROVIDER
        assert rework.model_override == FREE_MODEL

        body = kb.get_task(conn, result["rework_card"]).body or ""
        assert "the pipeline drops the header" in body
        assert "memory leak in step 3" in body

        # Every reviewer_verdict row is machine-readable: task, parent, sha.
        verdicts = _reviewer_verdicts(conn, review)
        assert len(verdicts) == 1
        v = verdicts[0]
        assert v["verdict"] == "CHANGES_REQUIRED"
        assert v["task"] == review
        assert v["parent"] == _review_parent(conn, review)
        assert "sha" in v
        assert v.get("blockers") == ["the pipeline drops the header", "memory leak in step 3"]

        # A second run creates NO second card (idempotency key).
        second = close_review_loop(
            conn, review, "CHANGES_REQUIRED", REVIEW_SHAS["initial"],
            blockers=["dup blocker"],
        )
        assert second["idempotent"] is True
        assert second["rework_card"] == result["rework_card"]
        # fix + review + rework = 3 tasks total
        assert len(kb.list_tasks(conn)) == 3

    def test_rework_inherits_the_original_free_provider(self, conn):
        fix = _create_fix(conn)
        review = _create_review(conn, fix)
        close_review_loop(
            conn, review, "CHANGES_REQUIRED", REVIEW_SHAS["initial"],
            blockers=["wrong shape"],
        )
        rework = kb.get_task(conn, _first_rework(conn, review))
        assert rework.provider_override == FREE_PROVIDER
        assert rework.model_override == FREE_MODEL


def _first_rework(conn, review_id):
    rows = conn.execute(
        "SELECT child_id FROM task_links WHERE parent_id = ?", (review_id,)
    ).fetchall()
    assert rows
    return rows[0]["child_id"]


def _task_children_list(conn, review_id):
    """Return list of child task IDs for a review task."""
    rows = conn.execute(
        "SELECT child_id FROM task_links WHERE parent_id = ?", (review_id,)
    ).fetchall()
    return [r["child_id"] for r in rows]


class TestPass:
    def test_pass_creates_no_card_and_emits_reviewer_verdict(self, conn):
        fix = _create_fix(conn)
        review = _create_review(conn, fix)
        result = close_review_loop(
            conn, review, "PASS", REVIEW_SHAS["initial"],
            created_by="user", from_envelope="hermes:systems-engineer",
        )
        assert result["idempotent"] is False
        assert result["verdict"] == "PASS"
        assert result["rework_card"] is None
        assert result["rework_cards"] == 0
        assert _block_kind(conn, review) is None

        verdicts = _reviewer_verdicts(conn, review)
        assert len(verdicts) == 1
        assert verdicts[0]["verdict"] == "PASS"
        assert verdicts[0]["parent"] == _review_parent(conn, review)

        fix_task = kb.get_task(conn, fix)
        assert fix_task.status == "done"

    def test_pass_with_unknown_issuer_blocks_origin_unreachable(self, conn):
        fix = _create_fix(conn)
        review = _create_review(conn, fix)
        result = close_review_loop(
            conn, review, "PASS", REVIEW_SHAS["initial"],
            created_by="user", from_envelope=None,
        )
        assert result["block_reason"] == "ORIGIN_UNREACHABLE (ISSUER_UNKNOWN)"
        assert _block_kind(conn, review) == "ORIGIN_UNREACHABLE (ISSUER_UNKNOWN)"


class TestGenerationCap:
    def test_stops_at_three_and_fourth_sets_loop_exhausted(self, conn):
        fix = _create_fix(conn)
        review = _create_review(conn, fix)
        expected_gen = 1
        for verdict, sha in (
            ("CHANGES_REQUIRED", REVIEW_SHAS["first"]),
            ("CHANGES_REQUIRED", REVIEW_SHAS["second"]),
            ("CHANGES_REQUIRED", REVIEW_SHAS["third"]),
        ):
            r = close_review_loop(conn, review, verdict, sha,
                                  blockers=["blocker %d" % i for i in range(1)])
            assert r["rework_card"] and r["generation"] == expected_gen
            expected_gen += 1

        # The 4th CHANGES_REQUIRED must not create a fourth card.
        exhausted = close_review_loop(
            conn, review, "CHANGES_REQUIRED", REVIEW_SHAS["fourth"],
            blockers=["blocker 4"],
        )
        assert exhausted["verdict"] == "CHANGES_REQUIRED"
        assert exhausted["block_reason"] == "LOOP_EXHAUSTED"
        assert _block_kind(conn, review) == "LOOP_EXHAUSTED"
        reworks = _task_children_list(conn, review)
        assert len(reworks) == 3
        assert all(kb.get_task(conn, t).status != "blocked" for t in reworks)

    def test_blocked_verdict_stop_chain(self, conn):
        fix = _create_fix(conn)
        review = _create_review(conn, fix)
        result = close_review_loop(
            conn, review, "BLOCKED", REVIEW_SHAS["initial"], reason="waiting on reviewer",
        )
        assert result["verdict"] == "BLOCKED"
        assert result["rework_card"] is None
        verdicts = _reviewer_verdicts(conn, review)
        assert len(verdicts) == 1
        assert verdicts[0]["verdict"] == "BLOCKED"
        assert verdicts[0]["task"] == review


class TestResolveIssuer:
    def test_user_is_issuer_unknown(self):
        assert resolve_issuer("user", None) == ("user", False)
        # created_by="user" falls back to envelope when available
        assert resolve_issuer("user", "hermes:systems-engineer") == ("hermes:systems-engineer", True)

    def test_role_name_is_known(self):
        assert resolve_issuer("hermes:systems-engineer", None) == (
            "hermes:systems-engineer", True)


# ---------------------------------------------------------------------------
# New tests for the three defects fixed in REWORK t_98942e8e
# ---------------------------------------------------------------------------

class TestGenerationCounterBypass:
    """Defect 1: Archived cards count toward cap but excluded from idempotency check."""

    def test_archive_rework_then_fourth_changes_required_yields_loop_exhausted(self, conn):
        """
        Archive a rework card after generation 1, then a 4th CHANGES_REQUIRED
        must yield LOOP_EXHAUSTED. This must FAIL if the fix is reverted.
        """
        fix = _create_fix(conn)
        review = _create_review(conn, fix)

        # Generation 1
        r1 = close_review_loop(conn, review, "CHANGES_REQUIRED", REVIEW_SHAS["first"],
                               blockers=["blocker 1"])
        assert r1["rework_card"] and r1["generation"] == 1
        rework1_id = r1["rework_card"]

        # Archive the gen-1 rework card
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (rework1_id,))

        # Generation 2
        r2 = close_review_loop(conn, review, "CHANGES_REQUIRED", REVIEW_SHAS["second"],
                               blockers=["blocker 2"])
        assert r2["rework_card"] and r2["generation"] == 2

        # Generation 3
        r3 = close_review_loop(conn, review, "CHANGES_REQUIRED", REVIEW_SHAS["third"],
                               blockers=["blocker 3"])
        assert r3["rework_card"] and r3["generation"] == 3

        # 4th CHANGES_REQUIRED must yield LOOP_EXHAUSTED
        exhausted = close_review_loop(conn, review, "CHANGES_REQUIRED", REVIEW_SHAS["fourth"],
                                      blockers=["blocker 4"])
        assert exhausted["verdict"] == "CHANGES_REQUIRED"
        assert exhausted["block_reason"] == "LOOP_EXHAUSTED"
        assert _block_kind(conn, review) == "LOOP_EXHAUSTED"

        # Only 3 rework cards total (gen-1 archived, gen-2, gen-3 active)
        reworks = _task_children_list(conn, review)
        assert len(reworks) == 3


class TestConcurrentCompletionsRace:
    """Defect 2: Race on concurrent completions - two reviewers creating rework cards."""

    def test_changes_required_called_twice_creates_exactly_one_rework_card(self, conn):
        """
        The CHANGES_REQUIRED path called twice on the same review (simulating
        concurrent completions) must leave exactly one rework card.
        """
        fix = _create_fix(conn)
        review = _create_review(conn, fix)

        # First call
        r1 = close_review_loop(conn, review, "CHANGES_REQUIRED", REVIEW_SHAS["first"],
                               blockers=["blocker 1"])
        assert r1["rework_card"]
        rework1_id = r1["rework_card"]

        # Second call with same SHA (simulating retry/concurrent completion)
        # Should return the SAME rework card, not create a new one
        r2 = close_review_loop(conn, review, "CHANGES_REQUIRED", REVIEW_SHAS["first"],
                               blockers=["blocker 1"])
        assert r2["rework_card"] == rework1_id
        assert r2["idempotent"] is True

        # Only one rework card should exist
        reworks = _task_children_list(conn, review)
        assert len(reworks) == 1
        assert reworks[0] == rework1_id


class TestAtomicEmission:
    """Defect 3: Non-atomic emission - verdict written before rework card exists."""

    def test_create_task_failure_rolls_back_verdict_event(self, conn, monkeypatch):
        """
        Force create_task to raise and assert no reviewer_verdict event was written.
        """
        fix = _create_fix(conn)
        review = _create_review(conn, fix)

        # Patch create_task to raise an exception
        original_create_task = kb.create_task
        def failing_create_task(*args, **kwargs):
            raise RuntimeError("simulated create_task failure")
        monkeypatch.setattr(kb, "create_task", failing_create_task)

        try:
            # This should raise, but the verdict event should NOT be written
            close_review_loop(conn, review, "CHANGES_REQUIRED", REVIEW_SHAS["first"],
                              blockers=["blocker 1"])
        except RuntimeError:
            pass  # Expected

        # Check that no reviewer_verdict event was written
        verdicts = _reviewer_verdicts(conn, review)
        assert len(verdicts) == 0, f"Expected no verdict events, got {len(verdicts)}: {verdicts}"

        # Restore
        monkeypatch.setattr(kb, "create_task", original_create_task)
