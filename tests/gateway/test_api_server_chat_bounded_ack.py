"""A chat:all caller is acknowledged inside the bound, never told a delivered message failed.

``authority.submit`` commits the user row, then the route waited for the whole turn (p50 37s,
p95 316s). A caller that budgets 10s reports the delivered message as a failure — and its
connector maps that outside-success answer to 502 "Invalid upstream response" — so a resend
duplicates the message. The bounded acknowledgement answers 202 with the committed admission id
(durable, non-retryable); only a ``chat:all`` scoped key can accept it. ``hermes peer dm`` sends
the master key with no scoped label, so it keeps its unbounded reply.
"""
import asyncio
import contextlib
import logging

import hermes_bootstrap  # noqa: F401  the drain imports it (via gateway.session_managed_worker);
# its install probe reads <home>/manifest.json, which the per-test home guard refuses once a test
# has started — so pay it at collection, like every other test that imports the agent stack.
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from types import SimpleNamespace

from gateway.config import PlatformConfig
from gateway.platforms import api_server
from gateway.platforms.api_server import APIServerAdapter
from gateway.session_authority import LiveSession, SessionAuthority
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch, list_session_admissions

PROFILE = "owned"
SESSION = "site"
SITE_LABEL = "chatgpt-site"
MASTER_KEY = "m" * 48
SITE_KEY = "s" * 48
USER_TEXT = "the site's private message text"


@pytest_asyncio.fixture
async def door(tmp_path, monkeypatch):
    """The real chat route, scoped-key gate, authority and DB; only the model turn is stubbed."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Production bound is 6s; a test cannot spend it. Absent today => the route blocks unbounded.
    monkeypatch.setattr(api_server, "_CHAT_ALL_ACK_BOUND_S", 0.05, raising=False)
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(SESSION, source="test")
    epoch = begin_runtime_epoch(db, instance_id="ack")
    turn = {"finishes": True}
    hold = asyncio.Event()

    async def execute(authority, ref, row):
        if not turn["finishes"]:
            await hold.wait()
        authority.pending_results[row["admission_id"]] = {
            "result": {"final_response": "settled reply", "messages": []}, "usage": {}}
        return "settled reply"

    monkeypatch.setattr("gateway.session_finite.execute_finite_admission", execute)
    runner = SimpleNamespace(_draining=False, config=SimpleNamespace(multiplex_profiles=False),
                             _adapter_for_source=lambda source: None)
    authority = SessionAuthority(runner, profile_id=PROFILE, instance_id="ack", db=db, epoch=epoch)
    authority.sessions[SESSION] = LiveSession(SimpleNamespace(platform=None, user_id="human"), "route")
    runner.session_authority = authority

    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter.gateway_runner = runner
    adapter._api_key = MASTER_KEY
    adapter._extra_api_keys = {SITE_LABEL: SITE_KEY}
    adapter._scoped_key_scopes = {SITE_LABEL: frozenset({"chat:all"})}
    app = web.Application(middlewares=[mw for mw in (
        getattr(api_server, "session_access_log_middleware", None),  # absent before the change
        api_server.scoped_key_guard_middleware) if mw is not None])
    app["api_server_adapter"] = adapter
    app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
    try:
        async with TestClient(TestServer(app)) as client:
            yield SimpleNamespace(client=client, db=db, authority=authority, adapter=adapter,
                                  turn=turn)
    finally:
        hold.set()  # a held turn settles rather than leaking its drain past the fixture
        task = authority.sessions[SESSION].task
        if task is not None and not task.done():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(task), 5)
        adapter._response_store.close()
        adapter._run_idempotency_store.close()
        db.close()


def _committed(db):
    """Every committed admission row for the fixture session, at its durable identity."""
    return list_session_admissions(db, session_id=SESSION, pending_only=False)


async def _post(door, key, message_id, text=USER_TEXT):
    return await door.client.post(
        f"/api/sessions/{SESSION}/chat", json={"message": text, "message_id": message_id},
        headers={"Authorization": f"Bearer {key}"})


@pytest.mark.asyncio
async def test_turn_that_finishes_inside_the_bound_still_answers_the_completion(door):
    response = await _post(door, SITE_KEY, "finishes")

    assert response.status == 200, await response.text()
    body = await response.json()
    assert body["object"] == "hermes.session.chat.completion"
    assert body["message"]["content"] == "settled reply"
    assert "retryable" not in body  # a real answer, not a queued acknowledgement


@pytest.mark.asyncio
async def test_turn_past_the_bound_answers_202_queued_with_the_committed_id(door):
    door.turn["finishes"] = False

    # Today this blocks until the turn ends (p95 316s), which the caller reports as a failure.
    response = await asyncio.wait_for(_post(door, SITE_KEY, "slow"), timeout=5)

    assert response.status == 202, await response.text()
    body = await response.json()
    assert body["object"] == "hermes.session.chat.queued"
    assert body["status"] == "queued"
    assert body["session_id"] == SESSION
    assert body["retryable"] is False  # committed: a resend would duplicate it
    assert "accepted" not in body and "message" not in body  # delivery is not claimed

    rows = _committed(door.db)
    assert [row["request_id"] for row in rows] == ["slow"]
    assert body["delivery_id"] == rows[0]["admission_id"]  # durable, lookable-up id


@pytest.mark.asyncio
async def test_a_queued_message_is_never_retryable_and_a_resend_commits_once(door):
    door.turn["finishes"] = False

    first = await asyncio.wait_for(_post(door, SITE_KEY, "once"), timeout=5)
    first_body = await first.json()
    assert first.status == 202 and first_body["retryable"] is False

    # The Site retries the same message id (its own idempotency key): same durable identity.
    second = await asyncio.wait_for(_post(door, SITE_KEY, "once"), timeout=5)
    assert second.status == 202
    assert [row["request_id"] for row in _committed(door.db)] == ["once"]


@pytest.mark.asyncio
async def test_peer_dm_still_waits_for_its_reply_past_the_bound(door, monkeypatch):
    adapter = door.adapter

    async def prepare(request):
        return {"gateway_session_key": "gk", "session_id": SESSION, "run_kwargs": {},
                "runtime_request": {}}, None

    async def run_agent(**_kwargs):
        await asyncio.sleep(0.3)  # six times the patched bound
        return {"final_response": "peer reply", "messages": []}, {}

    async def not_handed_off(_ctx):
        return None

    monkeypatch.setattr(adapter, "_prepare_session_chat", prepare)
    monkeypatch.setattr(adapter, "_run_agent", run_agent)
    monkeypatch.setattr(adapter, "_answer_through_live_bot_chat", not_handed_off)
    monkeypatch.setattr(adapter, "_effective_turn_runtime", lambda *a, **k: {})

    response = await asyncio.wait_for(_post(door, MASTER_KEY, "peer"), timeout=10)

    assert response.status == 200, await response.text()  # not a 202 because the bound passed
    body = await response.json()
    assert body["object"] == "hermes.session.chat.completion"
    assert body["message"]["content"] == "peer reply"
    assert _committed(door.db) == []  # the peer path never submits to the admission FIFO


@pytest.mark.asyncio
async def test_one_access_line_per_request_without_the_message_text(door, caplog):
    caplog.set_level(logging.INFO, logger="gateway.platforms.api_server")

    await _post(door, SITE_KEY, "logged")

    lines = [record.getMessage() for record in caplog.records
             if record.getMessage().startswith("api access route=")]
    assert len(lines) == 1, lines
    fields = dict(part.split("=", 1) for part in lines[0][len("api access "):].split(" "))
    assert fields["route"] == "/api/sessions/{session_id}/chat"
    assert fields["method"] == "POST"
    assert fields["status"] == "200"
    assert int(fields["duration_ms"]) >= 0
    assert fields["session"] == SESSION
    assert fields["delivery"] != "-"
    assert fields["label"] == SITE_LABEL

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert USER_TEXT not in text  # no message content
    assert SITE_KEY not in text  # no bearer
