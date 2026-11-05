"""Tests for the kanban dispatcher single-writer lock (issue #35240).

A ``hermes gateway run --replace`` / ``gateway restart`` from a shell on a
systemd/launchd host can leave an orphan dispatcher that escapes the
service cgroup, survives ``systemctl restart``, and becomes a second
long-lived writer on the same ``kanban.db`` — the documented root cause of
multi-writer SQLite WAL corruption. ``dispatch_once`` now wraps each tick in
a non-blocking, board-scoped dispatch lock so two dispatchers can never run
a reclaim/spawn/write tick concurrently. The losing dispatcher returns an
empty ``DispatchResult`` with ``skipped_locked=True`` and does no DB writes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c




def test_held_lock_skips_the_tick_without_writes(conn):
    """While another holder owns the board lock, dispatch_once must skip and
    must NOT invoke spawn_fn (no DB writes happen on a skipped tick)."""
    kb.create_task(conn, title="t", assignee="w")
    db_path = kb.kanban_db_path(board="default")

    spawn_calls: list = []

    def spy_spawn(task, workspace_path, board=None):
        spawn_calls.append(getattr(task, "id", task))
        return 999999

    # Hold the lock, then attempt a contended tick.
    with kbc._dispatch_tick_lock(db_path) as held:
        assert held is True  # we genuinely acquired it
        result = kbd.dispatch_once(conn, spawn_fn=spy_spawn)

    assert result.skipped_locked is True
    assert result.spawned == []
    assert spawn_calls == [], "spawn_fn must not run while the tick is locked out"




def test_lock_is_board_scoped(conn):
    """Holding board A's dispatch lock must not block a tick on board B —
    distinct boards have distinct DB files and tick independently."""
    db_default = kb.kanban_db_path(board="default")
    db_other = db_default.with_name("other-board-kanban.db")

    # Two different lock files → both acquirable simultaneously.
    with kbc._dispatch_tick_lock(db_default) as held_a:
        assert held_a is True
        with kbc._dispatch_tick_lock(db_other) as held_b:
            assert held_b is True, "a lock on a different board must be independent"





@pytest.mark.parametrize("caps,provider,admitted", [
    ({"openrouter": 0, "unset": 0}, "openrouter", False),
    ({"openrouter": 2, "unset": 0}, "openrouter", True),
    ({"unset": 0}, "unlisted-provider", False),
])
def test_provider_cap_admission(conn, kanban_home, monkeypatch, caps, provider, admitted):
    """Zero blocks; positive allows; missing provider uses literal unset."""
    import json
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda: "ok")
    caps_file = kanban_home / "state" / "kanban_lane_caps.json"
    caps_file.parent.mkdir(exist_ok=True)
    caps_file.write_text(json.dumps(caps), encoding="utf-8")
    tid = kb.create_task(
        conn, title="provider cap", assignee="worker",
        model_override="pinned-model", provider_override=provider,
    )
    result = kbd.dispatch_once(conn, dry_run=True)
    assert bool(result.spawned) is admitted
    assert bool(result.skipped_provider_capped) is not admitted
    if not admitted:
        assert result.skipped_provider_capped[0][0] == tid
        assert f"provider={provider}" in result.skipped_provider_capped[0][1]
        assert "cap=unset" in result.skipped_provider_capped[0][1] if provider not in caps else True


def test_provider_cap_resolves_assignee_without_override(conn, kanban_home, monkeypatch):
    """The formerly NULL provider cannot evade its assignee profile's cap."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda: "ok")
    profile = kanban_home / "profiles" / "worker"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "model:\n  default: test-model\n  provider: openrouter\n", encoding="utf-8",
    )
    monkeypatch.setattr(profiles, "resolve_profile_env", lambda _name: str(profile))
    caps_file = kanban_home / "state" / "kanban_lane_caps.json"
    caps_file.parent.mkdir(exist_ok=True)
    caps_file.write_text('{"openrouter": 0, "unset": 2}', encoding="utf-8")
    tid = kb.create_task(conn, title="null override", assignee="worker")
    result = kbd.dispatch_once(conn, dry_run=True)
    assert result.spawned == []
    assert result.skipped_provider_capped == [(tid, "provider=openrouter;cap=openrouter;cap_zero")]


@pytest.mark.parametrize("bad_caps", ["unreadable", "malformed"])
def test_provider_cap_bad_file_fails_closed(conn, kanban_home, monkeypatch, bad_caps):
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda: "ok")
    caps_file = kanban_home / "state" / "kanban_lane_caps.json"
    caps_file.parent.mkdir(exist_ok=True)
    if bad_caps == "unreadable":
        caps_file.mkdir()
    else:
        caps_file.write_text('{"openrouter": "zero"}', encoding="utf-8")
    tid = kb.create_task(
        conn, title="bad caps", assignee="worker",
        model_override="pinned-model", provider_override="openrouter",
    )
    result = kbd.dispatch_once(conn, dry_run=True)
    assert result.spawned == []
    assert result.skipped_provider_capped[0][0] == tid
    assert f"caps_{bad_caps}" in result.skipped_provider_capped[0][1]
