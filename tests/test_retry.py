# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Retry and backoff behaviour.

Ported from stream-daemon's test_ai_retry_logic.py when this code moved into
hypeman. Keeping the tests next to the implementation means Boon-Tube-Daemon
and Star-Daemon get the same guarantees, rather than one repo's suite silently
covering another repo's behaviour.
"""

from unittest.mock import patch

import pytest

from hypeman_social.llm.base import BaseLLM
from hypeman_social.llm.profiles import GENERIC_PROFILE


class ScriptedLLM(BaseLLM):
    """A provider that raises or returns whatever the test tells it to."""

    provider_name = 'scripted'

    def __init__(self, script):
        """
        Args:
            script: List of results per call. An Exception is raised, anything
                else is returned.
        """
        super().__init__(profile=GENERIC_PROFILE)
        self.script = list(script)
        self.calls = 0
        self.enabled = True
        self._configured = True
        self.reconnect_interval = 0

    def authenticate(self):
        return True

    def _reconnect(self):
        return True

    def _raw_generate(self, prompt, max_tokens):
        self.calls += 1
        outcome = self.script.pop(0) if self.script else "default result"
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def no_sleeping():
    """Backoff is real but tests shouldn't wait for it."""
    with patch('hypeman_social.llm.base.time.sleep') as mock:
        yield mock


# ─────────────────────────────────────────────────────────────────────────────
# Retryable conditions
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("error", [
    "503 Service Unavailable",
    "429 Too Many Requests",
    "quota exceeded",
    "model is overloaded",
    "read timed out",
])
def test_transient_errors_are_retried(error):
    """These all mean 'try again shortly', not 'give up'."""
    llm = ScriptedLLM([Exception(error), "recovered"])
    assert llm.generate("prompt") == "recovered"
    assert llm.calls == 2


def test_success_on_first_try_does_not_retry():
    llm = ScriptedLLM(["immediate"])
    assert llm.generate("prompt") == "immediate"
    assert llm.calls == 1


def test_max_retries_is_respected():
    llm = ScriptedLLM([Exception("503")] * 10)
    llm.max_retries = 3

    assert llm.generate("prompt") is None
    assert llm.calls == 3


def test_custom_max_retries_overrides_config():
    llm = ScriptedLLM([Exception("503")] * 10)
    assert llm.generate("prompt", max_retries=1) is None
    assert llm.calls == 1


def test_backoff_grows_exponentially(no_sleeping):
    """Hammering an overloaded server makes it worse."""
    llm = ScriptedLLM([Exception("503")] * 10)
    llm.max_retries = 4
    llm.retry_delay_base = 2

    llm.generate("prompt")

    delays = [call.args[0] for call in no_sleeping.call_args_list]
    assert delays == [2, 4, 8], f"expected doubling backoff, got {delays}"


# ─────────────────────────────────────────────────────────────────────────────
# Non-retryable conditions
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("error", [
    "model not found",
    "invalid argument",
    "unauthorized",
])
def test_permanent_errors_fail_fast(error):
    """Retrying a bad model name twenty times helps nobody."""
    llm = ScriptedLLM([Exception(error)] * 5)
    assert llm.generate("prompt") is None
    assert llm.calls == 1


def test_empty_response_is_not_retried():
    """An empty completion is an answer, just a useless one."""
    llm = ScriptedLLM([""])
    assert llm.generate("prompt") is None
    assert llm.calls == 1


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

def test_retry_settings_come_from_config(monkeypatch):
    monkeypatch.setenv('LLM_MAX_RETRIES', '7')
    monkeypatch.setenv('LLM_RETRY_DELAY_BASE', '3')

    llm = ScriptedLLM([])
    assert llm.max_retries == 7
    assert llm.retry_delay_base == 3


def test_thinking_mode_expands_the_token_budget(monkeypatch):
    """
    Reasoning models need headroom to think before they answer.

    Without the multiplier they spend the entire budget reasoning and return an
    empty completion.
    """
    monkeypatch.setenv('LLM_ENABLE_THINKING_MODE', 'true')
    monkeypatch.setenv('LLM_THINKING_TOKEN_MULTIPLIER', '5.0')
    monkeypatch.setenv('LLM_MAX_TOKENS', '150')

    seen = {}

    class Recorder(ScriptedLLM):
        def _raw_generate(self, prompt, max_tokens):
            seen['tokens'] = max_tokens
            return "ok"

    Recorder(["ok"]).generate("prompt")
    assert seen['tokens'] == 750


def test_thinking_mode_off_uses_the_plain_budget(monkeypatch):
    monkeypatch.setenv('LLM_ENABLE_THINKING_MODE', 'false')
    monkeypatch.setenv('LLM_MAX_TOKENS', '150')

    seen = {}

    class Recorder(ScriptedLLM):
        def _raw_generate(self, prompt, max_tokens):
            seen['tokens'] = max_tokens
            return "ok"

    Recorder(["ok"]).generate("prompt")
    assert seen['tokens'] == 150


# ─────────────────────────────────────────────────────────────────────────────
# Per-provider configuration
#
# A single LLM_MODEL cannot serve a primary and a fallback at once. Pointing
# Ollama at 'gemini-2.0-flash-lite' asks a local server for a model it has
# never heard of, and every generation quietly degrades to a template.
# ─────────────────────────────────────────────────────────────────────────────

def test_provider_specific_key_wins_over_the_shared_one(monkeypatch):
    monkeypatch.setenv('LLM_MODEL', 'gemini-2.0-flash-lite')
    monkeypatch.setenv('LLM_OLLAMA_MODEL', 'gemma3:12b')

    from hypeman_social.llm.ollama import OllamaLLM
    assert OllamaLLM().provider_config('model') == 'gemma3:12b'


def test_shared_key_is_still_honoured(monkeypatch):
    monkeypatch.delenv('LLM_OLLAMA_MODEL', raising=False)
    monkeypatch.setenv('LLM_MODEL', 'mistral:7b')

    from hypeman_social.llm.ollama import OllamaLLM
    assert OllamaLLM().provider_config('model') == 'mistral:7b'


def test_each_provider_reads_its_own_key(monkeypatch):
    """The whole point: primary and fallback name different models."""
    monkeypatch.setenv('LLM_OLLAMA_MODEL', 'gemma3:12b')
    monkeypatch.setenv('LLM_GEMINI_MODEL', 'gemini-2.0-flash-lite')

    from hypeman_social.llm.gemini import GeminiLLM
    from hypeman_social.llm.ollama import OllamaLLM

    assert OllamaLLM().provider_config('model') == 'gemma3:12b'
    assert GeminiLLM().provider_config('model') == 'gemini-2.0-flash-lite'


def test_default_applies_when_nothing_is_set(monkeypatch):
    monkeypatch.delenv('LLM_OLLAMA_MODEL', raising=False)
    monkeypatch.delenv('LLM_MODEL', raising=False)

    from hypeman_social.llm.ollama import OllamaLLM
    assert OllamaLLM().provider_config('model', default='fallback-model') == 'fallback-model'
