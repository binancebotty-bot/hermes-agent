"""Tests: kanban worker spawn pins TERMINAL_CWD to the task workspace.

Regression coverage for #34619 and #41312 (same root cause): ``_default_spawn``
launched the worker subprocess with ``cwd=workspace`` and set
``HERMES_KANBAN_WORKSPACE``, but did NOT set ``TERMINAL_CWD``. Because
``TERMINAL_CWD`` takes precedence over the process cwd in both
``tools/file_tools.py::_resolve_base_dir`` (relative ``write_file`` paths) and
``agent_init``'s context-file loader (``AGENTS.md`` discovery), workers inherited
the dispatching gateway's cwd — relative writes landed in the gateway user's
home (#41312) and the wrong profile's ``AGENTS.md`` was loaded (#34619).
Pinning ``TERMINAL_CWD`` to the workspace fixes both.
"""

from __future__ import annotations

import subprocess


def _make_task(kb, *, assignee: str = "w"):
    return kb.Task(
        id="t_cwd",
        title="cwd pin",
        body=None,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=1,
    )


def _capture_spawn_env(kb, monkeypatch, workspace: str) -> dict:
    from hermes_cli import kanban_db_dispatch as kbd

    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])

    captured: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    kbd._default_spawn(_make_task(kb), workspace)
    return captured


def test_terminal_cwd_pinned_to_workspace(monkeypatch, tmp_path):
    """A real, absolute workspace dir is pinned as TERMINAL_CWD."""
    root = tmp_path / ".hermes"
    (root / "profiles" / "w").mkdir(parents=True)
    (root / "profiles" / "w" / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    workspace = tmp_path / "ws"
    workspace.mkdir()

    captured = _capture_spawn_env(kb, monkeypatch, str(workspace))

    assert captured["env"]["TERMINAL_CWD"] == str(workspace)
    # The subprocess cwd and TERMINAL_CWD must agree — both anchor the workspace.
    assert captured["cwd"] == str(workspace)
    assert captured["env"]["HERMES_KANBAN_WORKSPACE"] == str(workspace)




def test_terminal_cwd_survives_owner_handoff_without_task_identity(monkeypatch, tmp_path):
    """The owner keeps the workspace anchor, never the worker's task identity."""
    import os
    from hermes_cli import kanban_worker_client as client

    workspace = str(tmp_path / "task-workspace")
    monkeypatch.setenv("TERMINAL_CWD", workspace)
    task_env = {
        "HERMES_KANBAN_BOARD": "test-board",
        "HERMES_KANBAN_TASK": "t_cwd",
        "HERMES_KANBAN_RUN_ID": "7",
        "HERMES_KANBAN_CLAIM_LOCK": "test-lock",
        "HERMES_KANBAN_DB": str(tmp_path / "board.db"),
        "HERMES_KANBAN_WORKSPACE": workspace,
        "HERMES_KANBAN_GOAL_MODE": "1",
        "HERMES_SESSION_SOURCE": "kanban",
        "HERMES_TENANT": "worker-tenant",
        "HERMES_KANBAN_HOME": str(tmp_path / "kanban-home"),
        "HERMES_KANBAN_BUSY_TIMEOUT_MS": "1000",
    }
    for key, value in task_env.items():
        monkeypatch.setenv(key, value)

    observed = {}

    def capture_owner_environment(_unused):
        observed.update(os.environ)
        return 0

    monkeypatch.setattr(client, "run", lambda _params, _board_db: None)
    monkeypatch.setattr(client.asyncio, "run", capture_owner_environment)
    assert client._submit() == 0
    assert observed["TERMINAL_CWD"] == workspace
    for key in ("HERMES_SESSION_SOURCE", "HERMES_TENANT"):
        assert key not in observed
    for key in task_env:
        if key.startswith("HERMES_KANBAN_") and key not in (
            "HERMES_KANBAN_HOME", "HERMES_KANBAN_BUSY_TIMEOUT_MS"
        ):
            assert key not in observed
    assert observed["HERMES_KANBAN_HOME"] == task_env["HERMES_KANBAN_HOME"]
    assert observed["HERMES_KANBAN_BUSY_TIMEOUT_MS"] == "1000"
