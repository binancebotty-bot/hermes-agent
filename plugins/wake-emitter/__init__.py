"""wake-emitter plugin — durable completion event producer.

Register with ``register(ctx)``. Feature flag is OFF by default; no background
execution, no transport of its own, and no guess at the originating pane.
"""
from .emitter import WakeEmitter, build_from_ctx, register, resolve_origin

__all__ = ["WakeEmitter", "build_from_ctx", "register", "resolve_origin"]
