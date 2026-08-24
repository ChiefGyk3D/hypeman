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
    m.fallbacks = [FakeLLM(server_up=True)]
    m.primary.provider_name = 'primary'
    m.fallbacks[0].provider_name = 'fallback'
    m.primary.authenticate()
    m.fallbacks[0].authenticate()
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
    for provider in (manager.primary, *manager.fallbacks):
        provider.server_up = False
        provider.mark_unavailable()
        provider.enable_auto_reconnect = False

    assert manager.is_available() is False
    assert manager.generate("prompt") is None


def test_works_with_no_fallback_configured(manager):
    """Fallback is opt-in; absence of one must not break anything."""
    manager.fallbacks = []
    assert manager.is_available() is True
    assert manager.generate("prompt") == "a generated message"


def test_status_exposes_both_providers(manager):
    status = manager.status()
    assert status['primary']['provider'] == 'primary'
    assert status['fallback']['provider'] == 'fallback'
    assert status['enabled'] is True


# ─────────────────────────────────────────────────────────────────────────────
# Ordered fallback chains
#
# Precedence is configuration, not code: LLM_FALLBACK_PROVIDER=gemini,anthropic
# tries each in the order given.
# ─────────────────────────────────────────────────────────────────────────────

def test_fallback_chain_is_parsed_in_order(monkeypatch):
    monkeypatch.setenv('LLM_FALLBACK_PROVIDER', 'gemini, anthropic ,openai')
    assert LLMManager._fallback_names('ollama') == ['gemini', 'anthropic', 'openai']


def test_fallback_chain_drops_the_primary_and_duplicates(monkeypatch):
    """Listing the primary as its own fallback should be harmless, not a loop."""
    monkeypatch.setenv('LLM_FALLBACK_PROVIDER', 'ollama,gemini,gemini')
    assert LLMManager._fallback_names('ollama') == ['gemini']


@pytest.mark.parametrize("value", ['', 'none', '  '])
def test_no_fallback_configured(monkeypatch, value):
    monkeypatch.setenv('LLM_FALLBACK_PROVIDER', value)
    assert LLMManager._fallback_names('ollama') == []


def test_chain_is_tried_in_order(manager):
    """Second fallback covers when the first is also down."""
    first, second = FakeLLM(server_up=False), FakeLLM(server_up=True)
    first.provider_name, second.provider_name = 'first', 'second'
    first.authenticate(); second.authenticate()
    first.enable_auto_reconnect = False

    manager.fallbacks = [first, second]
    manager.primary.server_up = False
    manager.primary.mark_unavailable()
    manager.primary.enable_auto_reconnect = False

    assert manager.is_available() is True
    assert manager.generate("prompt") == "a generated message"
    assert second.generate_calls >= 1


def test_fallback_property_returns_the_first_of_the_chain(manager):
    assert manager.fallback is manager.fallbacks[0]


def test_manager_heartbeat_probes_every_provider(manager):
    manager.primary.server_up = False
    manager.fallbacks[0].server_up = False

    assert manager.heartbeat(min_interval=0) is False
    assert manager.primary.enabled is False
    assert manager.fallbacks[0].enabled is False


def test_manager_heartbeat_notices_primary_recovery(manager):
    """Switch back to the local server as soon as it returns, not on next use."""
    manager.primary.server_up = False
    manager.primary.mark_unavailable()
    manager.is_available()
    assert manager.using_fallback is True

    manager.primary.server_up = True
    assert manager.heartbeat(min_interval=0) is True
    assert manager.using_fallback is False
