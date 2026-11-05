"""D6 invariant: typed block metadata must not survive a transition out of blocked.

These tests intentionally fail on the pinned b200f5bc base, where unblock_task
explicitly retains block_kind and block_recurrences.
"""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect() as conn:
        yield conn


def test_d6_unblock_clears_stale_block_metadata(board):
    tid = kb.create_task(board, title="D6 typed blocked")
    assert kb.claim_task(board, tid) is not None
    assert kb.block_task(board, tid, kind="needs_input", reason="fixture")
    before = kb.get_task(board, tid)
    assert before.status == "blocked"
    assert before.block_kind == "needs_input"
    assert kb.unblock_task(board, tid)
    after = kb.get_task(board, tid)
    assert after.status != "blocked"
    assert after.block_kind is None
    assert after.block_recurrences == 0


def test_d6_dependency_wait_does_not_leave_typed_nonblocked_status(board):
    tid = kb.create_task(board, title="D6 dependent")
    assert kb.claim_task(board, tid) is not None
    assert kb.block_task(board, tid, kind="dependency", reason="fixture")
    after = kb.get_task(board, tid)
    assert after.status != "blocked"
    assert after.block_kind is None
    assert after.block_recurrences == 0
