# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for provider failover — Ollama down should fall back, not degrade to templates."""

import pytest

from hypeman.llm.manager import LLMManager
from tests.test_availability import FakeLLM


@pytest.fixture
def manager():
    """A manager wired to two fake providers, bypassing config lookup."""
    m = LLMManager()
    m.primary = FakeLLM(server_up=True)
    m.fallback = FakeLLM(server_up=True)
    m.primary.provider_name = 'primary'
    m.fallback.provider_name = 'fallback'
    m.primary.authenticate()
    m.fallback.authenticate()
    m.enabled = True
    return m


def test_primary_serves_when_healthy(manager):
    assert manager.is_available() is True
    assert manager.generate("prompt") == "a generated message"
    assert manager.using_fallback is False
    assert manager.fallback.generate_calls == 0


def test_fallback_covers_a_downed_primary(manager):
    manager.primary.server_up = False
    manager.primary.mark_unavailable()

    assert manager.is_available() is True
    assert manager.generate("prompt") == "a generated message"
    assert manager.using_fallback is True
    assert manager.fallback.generate_calls >= 1


def test_primary_is_reclaimed_when_it_recovers(manager):
    """Don't keep paying Google after the local box comes back."""
    manager.primary.server_up = False
    manager.primary.mark_unavailable()
    manager.is_available()
    assert manager.using_fallback is True

    manager.primary.server_up = True

    assert manager.is_available() is True
    assert manager.using_fallback is False, "must return to primary once it recovers"


def test_unavailable_when_everything_is_down(manager):
    """With no provider usable, the caller falls back to static templates."""
    for provider in (manager.primary, manager.fallback):
        provider.server_up = False
        provider.mark_unavailable()
        provider.enable_auto_reconnect = False

    assert manager.is_available() is False
    assert manager.generate("prompt") is None


def test_works_with_no_fallback_configured(manager):
    """Fallback is opt-in; absence of one must not break anything."""
    manager.fallback = None
    assert manager.is_available() is True
    assert manager.generate("prompt") == "a generated message"


def test_status_exposes_both_providers(manager):
    status = manager.status()
    assert status['primary']['provider'] == 'primary'
    assert status['fallback']['provider'] == 'fallback'
    assert status['enabled'] is True
