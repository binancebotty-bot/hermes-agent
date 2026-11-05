"""KANFIX1420 D1/D2: author tests before any matching production change."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect() as conn:
        yield conn


def test_d1_new_scratch_tasks_never_share_inherited_workspace(board):
    ids = [kb.create_task(board, title=f"child {i}", workspace_kind="scratch",
                          workspace_path="workspaces/shared") for i in range(2)]
    paths = [kb.get_task(board, tid).workspace_path for tid in ids]
    assert len(set(paths)) == 2
    assert all(tid in path for tid, path in zip(ids, paths))


def test_d2_provider_cap_two_refuses_third_and_unset():
    gate = dispatch._provider_admission_allowed
    def result():
        return SimpleNamespace(
            skipped_provider_caps_invalid=[], skipped_provider_unset_or_unknown=[],
            skipped_provider_quarantined=[], skipped_per_provider_capped=[]
        )
    over = result()
    assert not gate("third", "nvidia", over, {"nvidia": 2}, set(), True, {"nvidia": 2})
    assert over.skipped_per_provider_capped == [("third", "nvidia", 2)]
    unset = result()
    assert not gate("unassigned", "unset", unset, {"nvidia": 2}, set(), True, {})
    assert unset.skipped_provider_unset_or_unknown == ["unassigned"]


def test_d2_common_dispatch_once_rejects_overcap_on_manual_path(board, monkeypatch):
    """The manual CLI and gateway both enter dispatch_once. Test that real tick."""
    home = Path(__import__("os").environ["HERMES_HOME"])
    state = home / "state"
    state.mkdir(exist_ok=True)
    (state / "kanban_lane_caps.json").write_text(json.dumps({"nvidia": 2, "unset": 0}))
    monkeypatch.setattr(dispatch, "_profile_exists_fn", lambda: lambda _name: True)
    for i in range(2):
        tid = kb.create_task(board, title=f"running {i}", assignee="worker",
                             provider_override="nvidia", model_override="allowed")
        assert kb.claim_task(board, tid) is not None
    third = kb.create_task(board, title="third", assignee="worker",
                           provider_override="nvidia", model_override="allowed")
    outcome = dispatch.dispatch_once(board, dry_run=True, max_spawn=1,
                                     reconcile_orphans=False)
    assert not any(row[0] == third for row in outcome.spawned)
    assert (third, "nvidia", 2) in outcome.skipped_per_provider_capped
