"""Contract tests for the ``hermes_health`` bridge tool.

Fake gateway only: ``urllib.request.urlopen`` is stubbed, or a real localhost socket
accepts and never answers. No real gateway, no network egress.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest


def _load_bridge():
    path = Path(__file__).resolve().parents[1] / "bridge.py"
    spec = importlib.util.spec_from_file_location("mcp_serve_health_bridge_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses need a registered module
    spec.loader.exec_module(module)
    return module


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self, _n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


@pytest.fixture
def subject(monkeypatch):
    bridge = _load_bridge()
    seen = []

    def fake_urlopen(url, **kwargs):
        seen.append((url, kwargs))
        return _FakeResponse(200, b'{"status": "ok", "platform": "hermes-agent", "version": "9.9.9"}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return bridge, seen


def test_stubbed_gateway_reports_ok_with_latency(subject):
    bridge, seen = subject
    result = bridge._probe_gateway_health()
    assert result["status"] == "ok"
    assert result["platform"] == "hermes-agent"
    assert result["version"] == "9.9.9"
    assert isinstance(result["latency_ms"], (int, float)) and result["latency_ms"] >= 0
    # the hard timeout is handed to the HTTP layer, never ignored
    assert seen[0][1]["timeout"] == bridge._GATEWAY_HEALTH_TIMEOUT_S


def test_refused_connection_returns_truthful_error(subject):
    bridge, _seen = subject

    def refuse(url, **kwargs):
        raise ConnectionRefusedError("refused")

    import urllib.request
    from unittest.mock import patch

    with patch.object(urllib.request, "urlopen", refuse):
        result = bridge._probe_gateway_health()  # must not raise
    assert result["status"] == "error"
    assert "unreachable" in result["error"]
    assert "refused" in result["error"]


def test_hanging_gateway_returns_within_timeout(monkeypatch):
    """A server that accepts and never answers must not hang the caller."""
    bridge = _load_bridge()
    monkeypatch.setattr(bridge, "_GATEWAY_HEALTH_TIMEOUT_S", 0.3)

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    held = []
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                server.settimeout(0.2)
                conn, _addr = server.accept()
                held.append(conn)  # hold it open, never respond
            except socket.timeout:
                continue
            except OSError:
                break

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        result = bridge._probe_gateway_health(url=f"http://127.0.0.1:{port}/health")
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        server.close()
        thread.join(timeout=1)
        for conn in held:
            conn.close()

    assert result["status"] == "error"
    assert elapsed < 2.0, f"hung gateway call took {elapsed:.2f}s, timeout was 0.3s"


def test_non_ok_payload_is_reported_as_error(monkeypatch):
    bridge = _load_bridge()

    class Bad:
        status = 503

        def read(self, _n=-1):
            return b'{"status": "starting"}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda url, **kwargs: Bad())
    result = bridge._probe_gateway_health()
    assert result["status"] == "error"
    assert "503" in result["error"]


def test_hermes_health_is_registered_and_listed(require_mcp_2_sdk):
    bridge = _load_bridge()
    assert "hermes_health" in bridge._TOOL_NAMES
    server = bridge.create_mcp_server(event_bridge=bridge.EventBridge())
    listed = {t.name for t in asyncio.run(server.list_tools())}
    assert "hermes_health" in listed


def test_hermes_health_tool_call_returns_json(subject, require_mcp_2_sdk):
    bridge, _seen = subject
    server = bridge.create_mcp_server(event_bridge=bridge.EventBridge())
    result = asyncio.run(server.call_tool("hermes_health", {}))
    text = "".join(getattr(b, "text", "") for b in (getattr(result, "content", None) or []))
    assert json.loads(text)["status"] == "ok"
