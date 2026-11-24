"""Focused tests for the read-only GET /api/sessions/{id}/turns/{delivery_id} route.

Every fixture uses a private ``state.db`` under pytest's ``tmp_path``; the running
profile's state.db is never opened here.
"""

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB

MASTER_KEY = "sk-turnstatus-master-0123456789abcdef"
SCOPED_KEY = "sk-turnstatus-scoped-0123456789abcdef"
RESULT_PREFIX = "gateway.admission.result.v1."
ROUTE_PATH = "/api/sessions/{session_id}/turns/{delivery_id}"


@pytest.fixture
def session_db(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        yield db
    finally:
        db.close()


def _adapter(session_db, *, extra=None):
    config_extra = {"key": MASTER_KEY}
    if extra:
        config_extra.update(extra)
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra=config_extra))
    adapter._session_db = session_db
    return adapter


def _app(adapter):
    app = web.Application()
    app.router.add_get(ROUTE_PATH, adapter._handle_session_turn_status)
    return app


def _auth(key=MASTER_KEY):
    return {"Authorization": "Bearer " + key}


def _create_session(db, session_id, *, pinned=False):
    db.create_session(session_id, "api_server")
    if pinned:
        db._execute_write(
            lambda conn: conn.execute("UPDATE sessions SET pinned=1 WHERE id=?", (session_id,)))


def _admission(db, session_id, delivery_id, status, *, outcome=None):
    db._write_sql(
        "INSERT INTO session_admissions(admission_id, request_id, principal_id, target_session_id, "
        "lineage_json, payload_json, payload_digest, intent, status, outcome, owner_epoch, generation) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (delivery_id, "req-" + delivery_id, "api", session_id, json.dumps([session_id]),
         "{}", "digest", "queue", status, outcome, 1, 1))


def _result(db, delivery_id, payload):
    db._write_sql(
        "INSERT OR REPLACE INTO state_meta(key, value) VALUES(?,?)",
        (RESULT_PREFIX + delivery_id, json.dumps(payload)))


def test_turn_status_route_is_declared_and_scoped_allowlisted():
    """The handler is wired as a GET route and the scoped-key allowlist permits it."""
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": MASTER_KEY}))
    routes = [row for row in adapter._http_route_table() if row[1] == ROUTE_PATH]
    assert len(routes) == 1
    assert routes[0][0] == "GET"
    assert routes[0][2].__func__ is APIServerAdapter._handle_session_turn_status

    from types import SimpleNamespace
    request = SimpleNamespace(method="GET", path="/api/sessions/session-1/turns/delivery-1")
    assert APIServerAdapter._scoped_key_allows(request) is True
    request = SimpleNamespace(method="POST", path="/api/sessions/session-1/turns/delivery-1")
    assert APIServerAdapter._scoped_key_allows(request) is False


@pytest.mark.asyncio
async def test_turn_status_without_auth_is_401_not_404(session_db):
    _create_session(session_db, "s1")
    app = _app(_adapter(session_db))
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/api/sessions/s1/turns/d-1")
    assert resp.status == 401
    assert resp.status != 404


@pytest.mark.asyncio
async def test_cross_session_delivery_id_does_not_leak(session_db):
    _create_session(session_db, "own")
    _create_session(session_db, "other")
    _admission(session_db, "other", "d-other", "terminal", outcome="ok")
    _result(session_db, "d-other", {"secret": "OTHER_SESSION_PAYLOAD"})
    app = _app(_adapter(session_db))
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/api/sessions/own/turns/d-other", headers=_auth())
        text = await resp.text()
    assert resp.status == 404
    body = json.loads(text)
    assert body["status"] == "UNKNOWN"
    assert body["delivery_id"] == "d-other"
    assert body["session_id"] == "own"
    assert "OTHER_SESSION_PAYLOAD" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("stored,expected", [
    ("unknown", "UNKNOWN"),
    ("queued", "QUEUED"),
    ("started", "RUNNING"),
    ("terminal", "TERMINAL"),
])
async def test_every_turn_status_is_represented(session_db, stored, expected):
    delivery_id = "d-" + stored
    _create_session(session_db, "s1")
    _admission(session_db, "s1", delivery_id, stored,
               outcome="ok" if stored == "terminal" else None)
    app = _app(_adapter(session_db))
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get(f"/api/sessions/s1/turns/{delivery_id}", headers=_auth())
        body = await resp.json()
    assert resp.status == 200
    assert body["object"] == "hermes.session.chat.turn"
    assert body["status"] == expected
    assert body["delivery_id"] == delivery_id
    assert body["session_id"] == "s1"


@pytest.mark.asyncio
async def test_terminal_without_payload_is_explicit(session_db):
    _create_session(session_db, "s1")
    _admission(session_db, "s1", "d-empty", "terminal", outcome="ok")
    app = _app(_adapter(session_db))
    async with TestClient(TestServer(app)) as cli:
        body = await (await cli.get("/api/sessions/s1/turns/d-empty", headers=_auth())).json()
    assert body["status"] == "TERMINAL"
    assert body["result_available"] is False
    assert isinstance(body.get("reason"), str) and body["reason"]


@pytest.mark.asyncio
async def test_terminal_payload_is_returned_and_bounded(session_db):
    _create_session(session_db, "s1")
    _admission(session_db, "s1", "d-small", "terminal", outcome="ok")
    _result(session_db, "d-small", {"ok": True, "answer": "hello"})
    _admission(session_db, "s1", "d-huge", "terminal", outcome="ok")
    _result(session_db, "d-huge", {"blob": "x" * 70000})
    app = _app(_adapter(session_db))
    async with TestClient(TestServer(app)) as cli:
        small = await (await cli.get("/api/sessions/s1/turns/d-small", headers=_auth())).json()
        huge = await (await cli.get("/api/sessions/s1/turns/d-huge", headers=_auth())).json()
    assert small["result_available"] is True
    assert small["result"] == {"ok": True, "answer": "hello"}
    assert small["truncated"] is False
    assert huge["result_available"] is True
    assert huge["truncated"] is True
    assert isinstance(huge["result"], str)
    assert len(huge["result"].encode("utf-8")) <= 65536


@pytest.mark.asyncio
async def test_turn_status_get_is_idempotent_and_writes_nothing(session_db):
    _create_session(session_db, "s1")
    _admission(session_db, "s1", "d-ro", "terminal", outcome="ok")
    _result(session_db, "d-ro", {"answer": "hi"})

    def counts():
        return {
            "sessions": session_db._read_one("SELECT COUNT(*) FROM sessions")[0],
            "admissions": session_db._read_one("SELECT COUNT(*) FROM session_admissions")[0],
            "state_meta": session_db._read_one("SELECT COUNT(*) FROM state_meta")[0],
        }

    before = counts()
    app = _app(_adapter(session_db))
    async with TestClient(TestServer(app)) as cli:
        first = await (await cli.get("/api/sessions/s1/turns/d-ro", headers=_auth())).json()
        second = await (await cli.get("/api/sessions/s1/turns/d-ro", headers=_auth())).json()
    assert first == second
    assert counts() == before


@pytest.mark.asyncio
async def test_scoped_key_is_confined_to_its_own_session_and_admission(session_db):
    _create_session(session_db, "own", pinned=True)
    _create_session(session_db, "other", pinned=True)
    _admission(session_db, "own", "d-own", "terminal", outcome="ok")
    _admission(session_db, "other", "d-other", "terminal", outcome="ok")
    _result(session_db, "d-other", {"secret": "CROSS_SESSION_SECRET"})
    adapter = _adapter(session_db, extra={"extra_keys": {"reader": SCOPED_KEY}})
    app = _app(adapter)
    async with TestClient(TestServer(app)) as cli:
        own = await cli.get("/api/sessions/own/turns/d-own", headers=_auth(SCOPED_KEY))
        own_body = await own.json()
        cross = await cli.get("/api/sessions/own/turns/d-other", headers=_auth(SCOPED_KEY))
        cross_status = cross.status
        cross_text = await cross.text()
    assert own.status == 200
    assert own_body["status"] == "TERMINAL"
    assert cross_status == 404
    assert "CROSS_SESSION_SECRET" not in cross_text
