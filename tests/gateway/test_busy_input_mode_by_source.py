"""Per-source busy input mode override (``display.busy_input_mode_by_source``).

With no override set, busy-mode resolution must be identical to the pre-override
behavior (global ``display.busy_input_mode`` / routed profile snapshot). When the
override map is set, "ui" sources default to interrupt and "inter_agent" sources
(MCP / router / coordinator relays) default to queue, with explicit entries winning.
"""

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.run_config_loaders import GatewayConfigLoadersMixin
from gateway.session import SessionSource


def _make_runner(busy_mode="interrupt"):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._busy_input_mode = busy_mode
    return runner


def _source(platform: Platform) -> SessionSource:
    return SessionSource(platform=platform, chat_id="chat-test", user_id="user-1")


class TestBusySourceKind:
    def test_ui_kind_is_local(self):
        assert GatewayConfigLoadersMixin._busy_source_kind(_source(Platform.LOCAL)) == "ui"

    def test_inter_agent_kinds(self):
        for platform in (Platform.RELAY, Platform.API_SERVER, Platform.WEBHOOK):
            assert GatewayConfigLoadersMixin._busy_source_kind(_source(platform)) == "inter_agent"

    def test_messaging_platform_has_no_kind(self):
        assert GatewayConfigLoadersMixin._busy_source_kind(_source(Platform.TELEGRAM)) is None


class TestBusyModesBySourceFromConfig:
    def test_unset_or_null_map_is_off(self):
        for config in ({}, {"display": {}}, {"display": {"busy_input_mode_by_source": None}}):
            assert GatewayConfigLoadersMixin._busy_modes_by_source_from_config(config) == {}

    def test_empty_map_enables_kind_defaults(self):
        modes = GatewayConfigLoadersMixin._busy_modes_by_source_from_config(
            {"display": {"busy_input_mode_by_source": {}}}
        )
        assert modes == {"ui": "interrupt", "inter_agent": "queue"}

    def test_defaults_when_override_set(self):
        modes = GatewayConfigLoadersMixin._busy_modes_by_source_from_config(
            {"display": {"busy_input_mode_by_source": {"inter_agent": "queue"}}}
        )
        assert modes == {"ui": "interrupt", "inter_agent": "queue"}

    def test_explicit_entries_win(self):
        modes = GatewayConfigLoadersMixin._busy_modes_by_source_from_config(
            {"display": {"busy_input_mode_by_source": {"ui": "queue", "inter_agent": "steer"}}}
        )
        assert modes == {"ui": "queue", "inter_agent": "steer"}

    def test_invalid_entry_falls_back_to_kind_default(self):
        modes = GatewayConfigLoadersMixin._busy_modes_by_source_from_config(
            {"display": {"busy_input_mode_by_source": {"ui": "bogus"}}}
        )
        assert modes == {"ui": "interrupt", "inter_agent": "queue"}


class TestEffectiveBusyInputMode:
    def test_no_override_identical_to_today(self, monkeypatch):
        """Without the override key, every source kind resolves to the global mode."""
        monkeypatch.setattr(
            gateway_run, "_load_gateway_config",
            lambda: {"display": {"busy_input_mode": "queue"}},
        )
        runner = _make_runner(busy_mode="queue")
        for platform in (Platform.LOCAL, Platform.RELAY, Platform.API_SERVER,
                         Platform.WEBHOOK, Platform.TELEGRAM):
            assert runner._effective_busy_input_mode(_source(platform)) == "queue"

    def test_override_defaults_ui_interrupt_inter_agent_queue(self, monkeypatch):
        monkeypatch.setattr(
            gateway_run, "_load_gateway_config",
            lambda: {"display": {"busy_input_mode_by_source": {}}},
        )
        runner = _make_runner(busy_mode="queue")
        assert runner._effective_busy_input_mode(_source(Platform.LOCAL)) == "interrupt"
        assert runner._effective_busy_input_mode(_source(Platform.RELAY)) == "queue"
        assert runner._effective_busy_input_mode(_source(Platform.API_SERVER)) == "queue"

    def test_messaging_platforms_ignore_override(self, monkeypatch):
        """The override never changes ordinary messaging platforms."""
        monkeypatch.setattr(
            gateway_run, "_load_gateway_config",
            lambda: {"display": {"busy_input_mode_by_source": {"ui": "queue"}}},
        )
        runner = _make_runner(busy_mode="interrupt")
        assert runner._effective_busy_input_mode(_source(Platform.TELEGRAM)) == "interrupt"

    def test_explicit_entry_beats_kind_default(self, monkeypatch):
        monkeypatch.setattr(
            gateway_run, "_load_gateway_config",
            lambda: {"display": {"busy_input_mode_by_source": {"ui": "steer"}}},
        )
        runner = _make_runner(busy_mode="queue")
        assert runner._effective_busy_input_mode(_source(Platform.LOCAL)) == "steer"
