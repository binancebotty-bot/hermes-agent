"""Pool delegation strategy (``delegation.backend: pool``).

Why: the bundled ``default`` strategy routes EVERY ``delegate_task`` task from the single
``delegation`` config section, so a fan-out of six children all lands on one provider. The
operator's rule is at most two children per provider, which caps real throughput at two.

This strategy keeps that rule usable at scale: each task may name a ``lane`` (an enum), and each
lane is its own route - provider, model, endpoint and optional fallbacks. A task that names no
lane gets exactly the ``delegation`` section's route, i.e. today's behaviour, so installing this
changes nothing until a caller asks for a lane.

Configuration (all optional - built-in free lanes are used when unset):

    delegation:
      backend: pool
      backends:
        pool:
          lanes:
            nous-free:
              provider: nous
              model: meituan/longcat-2.5:free
              label: nous free
            openrouter-free:
              provider: openrouter
              model: poolside/laguna-s-2.1:free
            profile-paid:
              profile: paid          # route to a Hermes profile; its own config decides the model

Route vocabulary is core's (``agent/delegation_provider.py``): ``model``, ``provider``,
``base_url``, ``api_key``, ``api_mode``, ``request_overrides``, ``reasoning_effort``,
``fallback_providers``, ``profile``, ``label``, ``service_tier``. Unknown keys are dropped; a
``lane`` that is not declared refuses the task with an instruction rather than silently
inheriting (a silent fallback is how one provider gets oversubscribed).
"""

from __future__ import annotations

from typing import Any, Mapping

from agent.delegation_provider import DelegationProvider, DelegationRouteError

#: Free lanes used when ``delegation.backends.pool.lanes`` is unset. Two per provider is the
#: operator's ceiling; the lane count is not a concurrency cap in itself (core still owns the
#: spawn budget), it only says which route a task travels.
_BUILTIN_LANES: dict[str, dict[str, Any]] = {
    "nous-free": {
        "provider": "nous",
        "model": "meituan/longcat-2.5:free",
        "label": "nous free",
    },
    "openrouter-free": {
        "provider": "openrouter",
        "model": "poolside/laguna-s-2.1:free",
        "label": "openrouter free",
    },
    "opencode-free": {
        "provider": "opencode",
        "model": "space-bunny-free",
        "label": "opencode free",
    },
}

#: Route keys a lane may set (mirrors core's ROUTE_KEYS; anything else is ignored).
_ROUTE_KEYS = (
    "model", "provider", "base_url", "api_key", "api_mode", "request_overrides",
    "reasoning_effort", "service_tier", "fallback_providers", "profile", "label",
)

#: Keys of the ``delegation`` section that are not route fields.
_NON_ROUTE_KEYS = ("backends", "backend", "gateway_mode", "max_concurrent_children", "child_timeout_seconds",
                   "max_spawn_depth", "orchestrator_enabled", "subagent_auto_approve",
                   "independent_completions", "inherit_mcp_toolsets", "service_tier")


class PoolDelegationProvider(DelegationProvider):
    """One route per named lane; no lane means the ``delegation`` section's own route."""

    @property
    def name(self) -> str:
        return "pool"

    @property
    def display_name(self) -> str:
        return "Pool"

    @property
    def summary(self) -> str:
        return ("Each task may name a lane (its own provider/model); a task with no lane uses "
                "delegation.model / delegation.provider, so per-provider fan-out stays legal.")

    @property
    def route_keys(self) -> tuple[str, ...]:
        return ("lane",)

    def _lanes(self, config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        """Built-in lanes, overridden entry-by-entry by ``delegation.backends.pool.lanes``."""
        lanes = {k: dict(v) for k, v in _BUILTIN_LANES.items()}
        own = self.settings(config)
        declared = own.get("lanes") if isinstance(own, Mapping) else None
        if isinstance(declared, Mapping):
            for lane_name, entry in declared.items():
                if not isinstance(lane_name, str) or not lane_name.strip():
                    continue
                if isinstance(entry, Mapping):
                    clean = {k: entry[k] for k in _ROUTE_KEYS if k in entry}
                    lanes[lane_name] = clean
        return lanes

    def task_parameters(self, config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        names = sorted(self._lanes(config))
        if not names:
            return {}
        return {
            "lane": {
                "type": "string",
                "enum": names,
                "description": ("Which route this child runs on. Omit to inherit delegation.model / "
                                "delegation.provider. Use different lanes for sibling tasks so no "
                                "single provider is oversubscribed."),
            }
        }

    def description(self, config: Mapping[str, Any]) -> str:
        lines = ["Lanes (pass `lane` per task so siblings spread across providers):"]
        for lane_name in sorted(self._lanes(config)):
            entry = self._lanes(config)[lane_name]
            target = entry.get("profile") and ("profile %s" % entry["profile"]) or (
                "%s / %s" % (entry.get("provider", "?"), entry.get("model", "parent")))
            lines.append("  - %s: %s" % (lane_name, target))
        return "\n".join(lines)

    def route_task(self, task: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
        lane = task.get("lane")
        if lane in (None, ""):
            # No lane: behave exactly like the default strategy.
            return {k: v for k, v in config.items() if isinstance(k, str) and k not in _NON_ROUTE_KEYS}
        lanes = self._lanes(config)
        entry = lanes.get(str(lane))
        if entry is None:
            raise DelegationRouteError(
                "unknown lane %r: declare it under delegation.backends.pool.lanes or use one of %s"
                % (lane, ", ".join(sorted(lanes)) or "(none)"))
        route = {k: entry[k] for k in _ROUTE_KEYS if k in entry and entry[k] not in (None, "")}
        if not route:
            raise DelegationRouteError(
                "lane %r declares no route fields; set at least provider+model or profile" % (lane,))
        return route


def register(ctx) -> None:
    ctx.register_delegation_provider(PoolDelegationProvider())
