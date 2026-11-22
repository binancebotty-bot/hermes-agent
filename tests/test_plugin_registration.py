"""register(ctx) wiring: ``hermes mcp-serve`` goes through the public plugin API, and the plugin
registers no model tools (its tools live on the MCP wire, not in the agent's tool list)."""

from __future__ import annotations

import argparse
from pathlib import Path

import mcp_serve_plugin
from mcp_serve_plugin import bridge


class RecordingCtx:
    def __init__(self):
        self.cli, self.tools = [], []

    def register_cli_command(self, **kwargs):
        self.cli.append(kwargs)

    def register_tool(self, **kwargs):  # pragma: no cover - must never be called
        self.tools.append(kwargs)


def test_registers_only_the_mcp_serve_command():
    ctx = RecordingCtx()
    mcp_serve_plugin.register(ctx)
    assert ctx.tools == []
    (cmd,) = ctx.cli
    assert cmd["name"] == "mcp-serve"
    parser = argparse.ArgumentParser()
    cmd["setup_fn"](parser)
    defaults = parser.parse_args([])
    assert (defaults.tools, defaults.tools_only, defaults.verbose, defaults.accept_hooks) == (False, False, False, False)
    assert parser.parse_args(["--tools", "-v"]).tools is True
    assert parser.parse_args(["--tools-only"]).tools_only is True


def test_readme_documents_the_full_bridge_tool_surface():
    """The README's tool count and names must match the registered surface (eleven, incl. hermes_health)."""
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert len(bridge._TOOL_NAMES) == 11
    assert "eleven tool" in readme
    for name in bridge._TOOL_NAMES:
        assert f"`{name}`" in readme, f"README does not document {name}"
    health_row = next(line for line in readme.splitlines() if line.startswith("| `hermes_health`"))
    assert "health" in health_row.lower()
