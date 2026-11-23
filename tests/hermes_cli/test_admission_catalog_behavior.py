"""Focused tests for admission catalog lookup and failure behavior.

Stubs ``_model_exists_at_provider`` at the module boundary where needed so no
Hermes install changes are required.
"""

from __future__ import annotations

import logging

import pytest

from hermes_cli import kanban_db_dispatch as kbd


def test_admission_refused_importable_at_module_level():
    from hermes_cli.kanban_db_dispatch import AdmissionRefused

    exc = AdmissionRefused("m", "p", reason="r")
    assert exc.model == "m"
    assert exc.provider == "p"
    assert "AdmissionRefused" in str(exc)


def test_authoritative_catalog_hit_returns_true(monkeypatch):
    monkeypatch.setattr(kbd, "_admission_catalog_fetch", lambda provider: ("gpt-4", "gpt-3.5"))
    assert kbd._model_exists_at_provider("GPT-4", "openai") is True


def test_authoritative_catalog_confirmed_miss_returns_false(monkeypatch):
    monkeypatch.setattr(kbd, "_admission_catalog_fetch", lambda provider: ("gpt-4",))
    # openai is a closed/authoritative provider
    assert kbd._model_exists_at_provider("no-such-model", "openai") is False


def test_lookup_exception_fails_open(monkeypatch, caplog):
    def boom(provider):
        raise RuntimeError("network down")

    monkeypatch.setattr(kbd, "_admission_catalog_fetch", boom)
    with caplog.at_level(logging.DEBUG):
        assert kbd._model_exists_at_provider("m", "openai") is True


def test_unreachable_provider_empty_catalog_fails_open_with_debug(caplog, monkeypatch):
    # Simulate fetch returning empty tuple (unreachable) -> fail open.
    monkeypatch.setattr(kbd, "_admission_catalog_fetch", lambda provider: ())
    with caplog.at_level(logging.DEBUG):
        assert kbd._model_exists_at_provider("m", "openai") is True


def test_non_authoritative_provider_miss_fails_open(monkeypatch, caplog):
    monkeypatch.setattr(kbd, "_admission_catalog_fetch", lambda provider: ("a",))
    # 'ollama' not in closed providers -> non-authoritative miss, fail open + warning
    with caplog.at_level(logging.WARNING):
        assert kbd._model_exists_at_provider("nope", "ollama") is True


def test_stub_at_module_boundary(monkeypatch):
    # Demonstrate stubbing _model_exists_at_provider at module boundary.
    monkeypatch.setattr(kbd, "_model_exists_at_provider", lambda model, provider: False)
    assert kbd._model_exists_at_provider("x", "y") is False
