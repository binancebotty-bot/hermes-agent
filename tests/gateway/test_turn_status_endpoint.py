import asyncio
import contextlib
import json
from types import SimpleNamespace

import hermes_bootstrap  # noqa: F401
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms import api_server
from gateway.platforms.api_server import APIServerAdapter
from gateway.session_authority import LiveSession, SessionAuthority
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch, list_session_admissions

PROFILE = "owned"
SESSION = "site"
OTHER = "other"
SITE_LABEL = "chatgpt-site"
MASTER_KEY = "m" * 48
SITE_KEY = "s" * 48


@pytest_asyncio.fixture
async def door(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(SESSION, source="test")
    db.create_session(OTHER, source="test")
    epoch = begin_runtime_epoch(db, instance_id="turn-status")
    hold = asyncio.Event()

    async def execute(authority, ref, row):
        await hold.wait()
        authority.pending_results[row["admission_id"]] = {
            "result": {"final_response": "settled reply", "messages": []}, "usage": {}}
        return "settled reply"

    monkeypatch.setattr("gateway.session_finite.execute_finite_admission", execute)
    runner = SimpleNamespace(_draining=False, config=SimpleNamespace(multiplex_profiles=False),
                             _adapter_for_source=lambda source: None)
    authority = SessionAuthority(runner, profile_id=PROFILE, instance_id="turn-status", db=db, epoch=epoch)
    authority.sessions[SESSION] = LiveSession(SimpleNamespace(platform=None, user_id="human"), "route")
    runner.session_authority = authority
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter.gateway_runner = runner
    adapter._api_key = MASTER_KEY
    adapter._extra_api_keys = {SITE_LABEL: SITE_KEY, "wrong": "w" * 48}
    adapter._scoped_key_scopes = {SITE_LABEL: frozenset({"chat:all"}), "wrong": frozenset()}
    app = web.Application(middlewares=[api_server.scoped_key_guard_middleware])
    app["api_server_adapter"] = adapter
    app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
    app.router.add_get("/api/sessions/{session_id}/turns/{delivery_id}", adapter._handle_session_turn_status)
    try:
        async with TestClient(TestServer(app)) as client:
            yield SimpleNamespace(client=client, db=db, authority=authority, adapter=adapter, hold=hold)
    finally:
        hold.set()
        task = authority.sessions[SESSION].task
        if task is not None and not task.done():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(task), 5)
        adapter._response_store.close()
        adapter._run_idempotency_store.close()
        db.close()


async def _admit(door, request_id):
    response = await door.client.post(
        f"/api/sessions/{SESSION}/chat", json={"message": "hello", "message_id": request_id},
        headers={"Authorization": f"Bearer {SITE_KEY}"})
    assert response.status == 202, await response.text()
    return (await response.json())["delivery_id"]


async def _get(door, session_id, delivery_id, key=SITE_KEY):
    return await door.client.get(
        f"/api/sessions/{session_id}/turns/{delivery_id}",
        headers={"Authorization": f"Bearer {key}"})


@pytest.mark.asyncio
async def test_open_admission_is_queued_or_running(door):
    delivery_id = await _admit(door, "open")
    response = await _get(door, SESSION, delivery_id)
    body = await response.json()
    assert response.status == 200
    assert body["status"] in {"QUEUED", "RUNNING"}
    assert body["delivery_id"] == delivery_id


@pytest.mark.asyncio
async def test_completed_admission_returns_terminal_payload(door):
    delivery_id = await _admit(door, "done")
    door.hold.set()
    await asyncio.wait_for(door.authority.sessions[SESSION].task, 5)
    response = await _get(door, SESSION, delivery_id)
    body = await response.json()
    assert response.status == 200
    assert body["status"] == "TERMINAL"
    assert body["outcome"] == "completed"
    assert body["result"]["final_response"] == "settled reply"


@pytest.mark.asyncio
async def test_failed_terminal_admission(door):
    delivery_id = await _admit(door, "failed")
    admission = list_session_admissions(door.db, session_id=SESSION, pending_only=False)[0]
    door.db.set_session_admission_outcome(admission["admission_id"], outcome="failed")
    response = await _get(door, SESSION, delivery_id)
    assert response.status == 200
    body = await response.json()
    assert body["status"] == "TERMINAL" and body["outcome"] == "failed"


@pytest.mark.asyncio
async def test_unknown_delivery_is_not_found(door):
    response = await _get(door, SESSION, "missing")
    assert response.status == 404
    assert await response.json() == {"object": "hermes.session.chat.turn", "status": "UNKNOWN",
                                     "delivery_id": "missing", "session_id": SESSION}


@pytest.mark.asyncio
async def test_other_sessions_delivery_is_hidden(door):
    delivery_id = await _admit(door, "private")
    response = await _get(door, OTHER, delivery_id)
    assert response.status == 404
    body = await response.json()
    assert body == {"object": "hermes.session.chat.turn", "status": "UNKNOWN",
                    "delivery_id": delivery_id, "session_id": OTHER}
    assert "result" not in body and "outcome" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [MASTER_KEY, "w" * 48])
async def test_non_chat_all_caller_is_forbidden(door, key):
    response = await _get(door, SESSION, "missing", key)
    assert response.status == 403


@pytest.mark.asyncio
async def test_identical_reads_have_identical_bodies(door):
    delivery_id = await _admit(door, "repeat")
    one, two = await _get(door, SESSION, delivery_id), await _get(door, SESSION, delivery_id)
    assert (await one.json()) == (await two.json())


@pytest.mark.asyncio
async def test_terminal_without_payload_marks_unavailable(door):
    delivery_id = await _admit(door, "no-payload")
    admission = list_session_admissions(door.db, session_id=SESSION, pending_only=False)[0]
    door.db.set_session_admission_outcome(admission["admission_id"], outcome="completed")
    response = await _get(door, SESSION, delivery_id)
    body = await response.json()
    assert response.status == 200 and body["status"] == "TERMINAL"
    assert body["result_available"] is False
    assert "reason" in body
