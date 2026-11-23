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
from gateway.session_contract import SessionActor, SessionRef, Submission
from gateway.session_authority import LiveSession, SessionAuthority
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch, list_session_admissions

PROFILE = "owned"
SESSION = "site"
SITE_LABEL = "chatgpt-site"
MASTER_KEY = "m" * 48
SITE_KEY = "s" * 48
USER_TEXT = "the site's private message text"
ACTOR = SessionActor("human", PROFILE, frozenset({"session:submit", "session:control"}), "cli")
REF = SessionRef(PROFILE, SESSION)


@pytest_asyncio.fixture
async def door(tmp_path, monkeypatch):
    """The real chat route, scoped-key gate, authority and DB; only the model turn is stubbed."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Production bound is 6s; a test cannot spend it (0.5s still covers a real DB read + settle).
    monkeypatch.setattr(api_server, "_CHAT_ALL_ACK_BOUND_S", 0.5, raising=False)
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
                                  turn=turn, hold=hold)
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
        await asyncio.sleep(1.0)  # beyond the patched bound
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


@pytest.mark.asyncio
async def test_a_settled_admission_never_leaves_a_waiter_nothing_can_resolve(door):
    """Settlement racing the ack path must not orphan a future, and the settled answer must win.

    The caller's bound expires with the turn still running, so the route answers 202. Settlement
    pops and resolves whatever future happens to be registered, so an abandoned one is either
    unresolvable forever or adopted by the next caller's ``setdefault`` — which then waits on a
    future settlement has already passed and burns its whole bound instead of returning the reply.
    """
    door.turn["finishes"] = False
    first = await asyncio.wait_for(_post(door, SITE_KEY, "raced"), timeout=5)
    assert first.status == 202, await first.text()
    ticket = (await first.json())["delivery_id"]
    assert ticket not in door.authority.waiters, (
        "the acknowledged caller left a waiter that no settlement will resolve")

    # The running turn settles afterwards: the same committed message answers with its result.
    door.turn["finishes"] = True
    door.hold.set()
    for _ in range(200):
        if (door.authority.waiters.get(ticket) is None
                and _committed(door.db)[0]["status"] == "terminal"):
            break
        await asyncio.sleep(0.01)
    second = await asyncio.wait_for(_post(door, SITE_KEY, "raced"), timeout=5)
    assert second.status == 200, await second.text()
    assert (await second.json())["object"] == "hermes.session.chat.completion"


@pytest.mark.asyncio
@pytest.mark.allow_real_home_io  # this clone runs under the live interpreter tree
async def test_a_busy_steer_does_not_claim_the_message_was_accepted(door):
    """A steer aimed at a running turn is best effort: its only guarantee is the durable commit.

    ``accepted: true`` claimed the steer would be applied to the running generation, which the
    authority never promises (the transfer can fail and the admission simply queues). The caller
    acts on that claim, so the envelope must carry no acceptance and must be non-retryable — the
    message is already committed.
    """
    door.turn["finishes"] = False
    await door.authority.submit(ACTOR, Submission(request_id="running", ref=REF,
                                                 payload={"text": "first"}, intent="queue"))
    for _ in range(300):  # the drain must have claimed the turn before the steer is busy
        if door.authority._handle(REF).execution_state == "running":
            break
        await asyncio.sleep(0.01)
    assert door.authority._handle(REF).execution_state == "running", "the turn never started"
    response = await asyncio.wait_for(_post(door, SITE_KEY, "steer-me"), timeout=5)

    assert response.status == 202, await response.text()
    body = await response.json()
    assert body["object"] == "hermes.session.chat.steer", body  # the busy branch, not the idle ack
    assert "accepted" not in body, f"a best-effort steer claimed acceptance: {body}"
    assert body["retryable"] is False
    assert body["delivery_id"]
    assert [row["request_id"] for row in _committed(door.db)] == ["running", "steer-me"]


@pytest.mark.asyncio
@pytest.mark.allow_real_home_io  # this clone runs under the live interpreter tree
async def test_a_settled_turn_is_never_answered_queued_at_the_decision(door, monkeypatch):
    """The queued answer must rest on a read taken AT the decision, not on an earlier snapshot.

    Settlement can commit between the caller's row read and its deadline decision, so answering
    ``queued`` from that stale row tells the caller to give up on a turn that already finished —
    the exact false signal this route exists to remove.
    """
    monkeypatch.setattr(api_server, "_CHAT_ALL_ACK_BOUND_S", 0.0)  # deadline already expired
    import hermes_state_runtime as runtime
    read_row = runtime.get_session_admission

    door.turn["finishes"] = False
    first = await asyncio.wait_for(_post(door, SITE_KEY, "settling"), timeout=5)
    assert first.status == 202, await first.text()
    ticket = (await first.json())["delivery_id"]
    door.turn["finishes"] = True
    door.hold.set()
    for _ in range(300):
        if read_row(door.db, admission_id=ticket)["status"] == "terminal":
            break
        await asyncio.sleep(0.01)
    settled = read_row(door.db, admission_id=ticket)
    assert settled["status"] == "terminal"

    # The caller's admission read returns the pre-settlement snapshot: settlement lands while the
    # read is in flight, which is exactly the window the decision must not trust. Every later read
    # (the decision's own) is the live row. ``to_thread`` is wrapped rather than the module
    # function so only the helper's own read of this row is made stale.
    real_to_thread = asyncio.to_thread
    reads = {"n": 0}

    async def to_thread(func, *args, **kwargs):
        if func is read_row and kwargs.get("admission_id") == ticket:
            reads["n"] += 1
            result = await real_to_thread(func, *args, **kwargs)
            if reads["n"] == 1:
                return {**result, "status": "started", "outcome": None}
            return result
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", to_thread)

    second = await asyncio.wait_for(_post(door, SITE_KEY, "settling"), timeout=5)
    body = await second.json()
    assert second.status == 200, f"a settled turn was answered queued: {body}"
    assert body["object"] == "hermes.session.chat.completion", body


@pytest.mark.asyncio
@pytest.mark.allow_real_home_io  # this clone runs under the live interpreter tree
async def test_a_turn_settling_right_now_is_never_answered_queued(door, monkeypatch):
    """Settlement commits outside the waiter lock, so ``live.settling`` is the in-flight signal.

    A stale non-terminal read while the ticket is marked settling means the commit is imminent:
    answering ``queued`` there tells the caller to give up on a turn that is already finishing.
    """
    monkeypatch.setattr(api_server, "_CHAT_ALL_ACK_BOUND_S", 0.0)  # decision has no budget left
    import hermes_state_runtime as runtime
    read_row = runtime.get_session_admission
    live = door.authority.sessions[SESSION]

    door.turn["finishes"] = False
    first = await asyncio.wait_for(_post(door, SITE_KEY, "inflight"), timeout=5)
    assert first.status == 202, await first.text()
    ticket = (await first.json())["delivery_id"]
    door.turn["finishes"] = True
    door.hold.set()
    for _ in range(300):
        if read_row(door.db, admission_id=ticket)["status"] == "terminal":
            break
        await asyncio.sleep(0.01)
    settled = read_row(door.db, admission_id=ticket)
    assert settled["status"] == "terminal"
    for _ in range(300):  # the ack path only runs once the session reads idle (else it steers)
        if door.authority._handle(REF).execution_state == "idle":
            break
        await asyncio.sleep(0.01)
    assert door.authority._handle(REF).execution_state == "idle", "session never went idle"

    stale = {**settled, "status": "started", "outcome": None}
    with live.event_stream.lock:
        live.settling[ticket] = stale  # settlement begun: commit + publish in flight
    reads = {"n": 0}
    real_to_thread = asyncio.to_thread

    async def to_thread(func, *args, **kwargs):
        if func is read_row and kwargs.get("admission_id") == ticket:
            reads["n"] += 1
            if reads["n"] <= 2:  # the stale snapshot, including the decision's own re-read
                return stale
            with live.event_stream.lock:
                live.settling.pop(ticket, None)  # it published between those reads
            return settled
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", to_thread)
    try:
        # The ack decision itself: the route's busy branch is a different path (it steers), and this
        # defect lives here. The helper must not answer "queued" for a ticket settlement is finishing.
        row, queued = await door.adapter._await_chat_all_settlement(door.authority, SESSION, ticket)
        assert queued is False, "a settling turn was answered queued"
        assert row["status"] == "terminal", row
    finally:
        with live.event_stream.lock:
            live.settling.pop(ticket, None)


@pytest.mark.asyncio
@pytest.mark.allow_real_home_io  # this clone runs under the live interpreter tree
async def test_a_commit_that_pops_the_marker_between_read_and_check_is_not_queued(door, monkeypatch):
    """Race C: the read is stale, the marker is already clear, and the commit has landed.

    Settlement commits *and* pops ``settling`` between the caller's read and its marker check, so
    the marker proves nothing about freshness: the decision must re-read once more, or it answers
    "queued" for a turn that has already settled.
    """
    monkeypatch.setattr(api_server, "_CHAT_ALL_ACK_BOUND_S", 0.0)
    import hermes_state_runtime as runtime
    read_row = runtime.get_session_admission
    live = door.authority.sessions[SESSION]

    door.turn["finishes"] = False
    first = await asyncio.wait_for(_post(door, SITE_KEY, "race-c"), timeout=5)
    assert first.status == 202, await first.text()
    ticket = (await first.json())["delivery_id"]
    door.turn["finishes"] = True
    door.hold.set()
    for _ in range(300):
        if read_row(door.db, admission_id=ticket)["status"] == "terminal":
            break
        await asyncio.sleep(0.01)
    settled = read_row(door.db, admission_id=ticket)
    assert settled["status"] == "terminal"
    with live.event_stream.lock:
        live.settling.pop(ticket, None)  # the commit popped it: marker check will be clear

    stale = {**settled, "status": "started", "outcome": None}
    reads = {"n": 0}
    real_to_thread = asyncio.to_thread

    async def to_thread(func, *args, **kwargs):
        if func is read_row and kwargs.get("admission_id") == ticket:
            reads["n"] += 1
            return stale if reads["n"] <= 2 else settled  # stale read, then the commit landed
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", to_thread)
    row, queued = await door.adapter._await_chat_all_settlement(door.authority, SESSION, ticket)
    assert queued is False, "queued was answered for a turn whose commit had already landed"
    assert row["status"] == "terminal", row


@pytest.mark.asyncio
@pytest.mark.allow_real_home_io  # this clone runs under the live interpreter tree
async def test_the_settling_marker_is_popped_once_published(door):
    """Production marker cleanup: ``live.settling`` empties once the completion is published."""
    door.turn["finishes"] = False
    first = await asyncio.wait_for(_post(door, SITE_KEY, "marker"), timeout=5)
    assert first.status == 202, await first.text()
    ticket = (await first.json())["delivery_id"]
    live = door.authority.sessions[SESSION]
    door.turn["finishes"] = True
    door.hold.set()
    for _ in range(300):
        if _committed(door.db)[0]["status"] == "terminal" and not live.settling:
            break
        await asyncio.sleep(0.01)
    assert _committed(door.db)[0]["status"] == "terminal"
    assert ticket not in live.settling, live.settling
    task = getattr(live, "task", None)
    if task is not None and not task.done():
        await asyncio.wait_for(asyncio.shield(task), 5)
    assert not live.settling, live.settling


@pytest.mark.asyncio
@pytest.mark.allow_real_home_io  # this clone runs under the live interpreter tree
async def test_spin_exhaustion_answers_queued_instead_of_hanging(door, monkeypatch):
    """A marker that never clears must exhaust the spins and answer queued, never hang."""
    monkeypatch.setattr(api_server, "_CHAT_ALL_ACK_BOUND_S", 0.0)
    import time

    import hermes_state_runtime as runtime
    read_row = runtime.get_session_admission
    live = door.authority.sessions[SESSION]

    door.turn["finishes"] = False
    first = await asyncio.wait_for(_post(door, SITE_KEY, "spins"), timeout=5)
    assert first.status == 202, await first.text()
    ticket = (await first.json())["delivery_id"]
    started = time.monotonic()

    stale = {**read_row(door.db, admission_id=ticket), "status": "started", "outcome": None}
    real_to_thread = asyncio.to_thread

    async def to_thread(func, *args, **kwargs):
        if func is read_row and kwargs.get("admission_id") == ticket:
            with live.event_stream.lock:
                live.settling[ticket] = stale  # never cleared: the bounded spins must end this
            return stale
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", to_thread)
    try:
        row, queued = await asyncio.wait_for(
            door.adapter._await_chat_all_settlement(door.authority, SESSION, ticket), timeout=5)
        assert queued is True, row
        assert row["status"] != "terminal"
        assert time.monotonic() - started < 1.0, "the bounded spins must finish quickly"
    finally:
        with live.event_stream.lock:
            live.settling.pop(ticket, None)
