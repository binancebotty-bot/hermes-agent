"""Tests for the review completion wiring.

These tests verify that the close_review_loop function is properly wired into:
1. complete_task when a review is completed (PASS verdict)
2. request_changes when a reviewer rejects (CHANGES_REQUIRED verdict)
3. Proper verdict parsing from worker output
4. Idempotency and edge cases
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from hermes_cli import kanban_db_connect as kbc  # noqa: E402
from hermes_cli import kanban_db as kb  # noqa: E402
from hermes_cli.kanban_db import close_review_loop, is_review_shaped, claim_task  # noqa: E402

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
    review = kb.create_task(
        c, title="REVIEW the fix", body="review this", assignee=assignee,
        parents=(fix_id,),
    )
    # Claim the review task
    claim = kb.claim_task(c, review, claimer=kb._claimer_id())
    assert claim is not None
    
    # Request review (this puts it in "review" status)
    kb.request_review(c, review, summary="Ready for review")
    
    assert kb.get_task(c, review).status == "review"
    return review


def _reviewer_verdicts(c, task_id):
    out = []
    for ev in kb.list_events(c, task_id):
        if ev.kind == "reviewer_verdict":
            out.append(ev.payload)
    return out


# ---------------------------------------------------------------------------  
# Test matrix
# ---------------------------------------------------------------------------

class TestCompleteTaskWiring:
    """Test that complete_task wires close_review_loop for review completion."""
    
    def test_review_completion_emits_pass_verdict(self, conn):
        """When a review task is completed, it should emit a PASS reviewer_verdict event."""
        fix = _create_fix(conn)
        review = _create_review(conn, fix)
        
        # Complete the review task with PASS verdict in the result
        success = kb.complete_task(
            conn, review, 
            result="The fix looks good.\\n\\nREVIEWER_VERDICT: PASS",
            summary="Approved with minor suggestions",
        )
        
        assert success, f"complete_task failed"
        
        # Check that a reviewer_verdict event was emitted
        verdicts = _reviewer_verdicts(conn, review)
        assert len(verdicts) == 1
        verdict = verdicts[0]
        assert verdict["verdict"] == "PASS"
        assert verdict["task"] == review
        assert verdict["parent"] == fix
        assert "sha" in verdict
        
        # Check that the fix parent is marked as done
        fix_task = kb.get_task(conn, fix)
        assert fix_task.status == "done"
    
    def test_review_completion_without_verdict_marker_does_not_pass(self, conn):
        """When a review task is completed without a verdict marker, it should not become a pass."""
        fix = _create_fix(conn)
        review = _create_review(conn, fix)
        
        # Complete the review task without any verdict marker
        success = kb.complete_task(
            conn, review,
            result="The fix looks good, but I need to think more about it.",
            summary="Needs more consideration",
        )
        
        assert success, f"complete_task failed"
        
        # Check that NO reviewer_verdict event was emitted
        verdicts = _reviewer_verdicts(conn, review)
        assert len(verdicts) == 0
        
        # The review should not be marked as done (it should stay in some other state)
        review_task = kb.get_task(conn, review)
        # The exact status depends on the implementation, but it shouldn't be "done"
        assert review_task.status != "done"
    
    def test_review_completion_with_blocked_verdict(self, conn):
        """When a review task is completed with BLOCKED verdict, it should emit BLOCKED event."""
        fix = _create_fix(conn)
        review = _create_review(conn, fix)
        
        # Complete the review task with BLOCKED verdict
        success = kb.complete_task(
            conn, review,
            result="This fix is blocked by external dependencies.\n\nREVIEWER_VERDICT: BLOCKED",
            summary="Blocked by external dependencies",
        )
        
        assert success, f"complete_task failed"
        
        # Check that a BLOCKED reviewer_verdict event was emitted
        verdicts = _reviewer_verdicts(conn, review)
        assert len(verdicts) == 1
        verdict = verdicts[0]
        assert verdict["verdict"] == "BLOCKED"
        assert verdict["task"] == review
        assert verdict["parent"] == fix


class TestRequestChangesWiring:
    """Test that request_changes wires close_review_loop for review rejection."""
    
    def test_request_changes_emits_changes_required_and_creates_rework_card(self, conn):
        """When request_changes is called on a review, it should emit CHANGES_REQUIRED and create rework card."""
        fix = _create_fix(conn)
        review = _create_review(conn, fix)
        
        # Request changes on the review
        success, implementer = kb.request_changes(
            conn, review,
            reason="The fix needs changes:\\n\\n1. Fix the memory leak\\n2. Add tests",
        )
        
        assert success is True
        assert implementer is not None
        
        # Check that a CHANGES_REQUIRED reviewer_verdict event was emitted
        verdicts = _reviewer_verdicts(conn, review)
        assert len(verdicts) == 1
        verdict = verdicts[0]
        assert verdict["verdict"] == "CHANGES_REQUIRED"
        assert verdict["task"] == review
        assert verdict["parent"] == fix
        assert verdict.get("blockers") == ["The fix needs changes:", "", "1. Fix the memory leak", "2. Add tests"]
        
        # Check that a rework card was created
        children = kb._task_children(conn, review)
        assert len(children) == 1
        
        rework = kb.get_task(conn, children[0])
        assert rework.title.startswith("REWORK")
        assert review in [link["parent_id"] for link in kb._task_children_list(conn, review)]
        assert rework.provider_override == FREE_PROVIDER
        assert rework.model_override == FREE_MODEL


class TestIdempotency:
    """Test that repeated completions don't create duplicate events or cards."""
    
    def test_completing_same_review_twice_emits_only_one_event(self, conn):
        """Completing the same review twice should emit only one reviewer_verdict event."""
        fix = _create_fix(conn)
        review = _create_review(conn, fix)
        
        # Complete the review task first time
        success1 = kb.complete_task(
            conn, review,
            result="Looks good.\\n\\nREVIEWER_VERDICT: PASS",
            summary="Approved",
        )
        
        assert success1, f"complete_task failed"
        
        # Complete the review task second time (should be idempotent)
        success2 = kb.complete_task(
            conn, review,
            result="Looks good.\\n\\nREVIEWER_VERDICT: PASS",
            summary="Approved again",
        )
        
        assert success2, f"complete_task failed"
        
        # Check that only one reviewer_verdict event was emitted
        verdicts = _reviewer_verdicts(conn, review)
        assert len(verdicts) == 1


class TestVerdictParsing:
    """Test verdict parsing from worker output."""
    
    def test_verdict_marker_is_case_insensitive(self, conn):
        """Verdict marker should be case insensitive."""
        fix = _create_fix(conn)
        review = _create_review(conn, fix)
        
        # Test with lowercase verdict
        success = kb.complete_task(
            conn, review,
            result="Approved.\\n\\nreviewer_verdict: pass",
            summary="Approved",
        )
        
        assert success, f"complete_task failed"
        verdicts = _reviewer_verdicts(conn, review)
        assert len(verdicts) == 1
        assert verdicts[0]["verdict"] == "PASS"
    
    def test_verdict_marker_with_surrounding_prose(self, conn):
        """Verdict marker should be parsed even with surrounding prose."""
        fix = _create_fix(conn)
        review = _create_review(conn, fix)
        
        # Test verdict marker in the middle of prose
        success = kb.complete_task(
            conn, review,
            result="After careful consideration of the implementation details and testing approach, "
                   "I conclude that this fix meets all requirements.\\n\\nREVIEWER_VERDICT: PASS\\n\\n"
                   "The code quality is excellent and follows our standards.",
            summary="Approved after careful review",
        )
        
        assert success, f"complete_task failed"
        verdicts = _reviewer_verdicts(conn, review)
        assert len(verdicts) == 1
        assert verdicts[0]["verdict"] == "PASS"
    
    def test_verdict_marker_with_whitespace(self, conn):
        """Verdict marker should tolerate surrounding whitespace."""
        fix = _create_fix(conn)
        review = _create_review(conn, fix)
        
        # Test verdict marker with extra whitespace
        success = kb.complete_task(
            conn, review,
            result="Approved.\\n\\n   REVIEWER_VERDICT:   PASS   \\n\\nGreat work!",
            summary="Approved",
        )
        
        assert success, f"complete_task failed"
        verdicts = _reviewer_verdicts(conn, review)
        assert len(verdicts) == 1
        assert verdicts[0]["verdict"] == "PASS"


class TestIsReviewShaped:
    """Test the is_review_shaped function used for detection."""
    
    def test_is_review_shaped_detects_review_tasks(self, conn):
        """is_review_shaped should return True for tasks marked as review."""
        # Create a regular task
        regular = kb.create_task(
            conn, title="Regular task", body="Do this work",
        )
        
        # Create a review task
        review = kb.create_task(
            conn, title="REVIEW the fix", body="[review] Please review this",
        )
        
        # Create another review task with [review] in title
        review2 = kb.create_task(
            conn, title="[review] Code review", body="Review this code",
        )
        
        assert is_review_shaped(conn, regular) is False
        assert is_review_shaped(conn, review) is True
        assert is_review_shaped(conn, review2) is True