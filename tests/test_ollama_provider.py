# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Ollama response handling.

Ported from stream-daemon's test_qwen3_thinking_mode.py. The ollama client
library has changed response shapes across versions and between its generate()
and chat() APIs, so unpacking has to tolerate all of them.
"""

from types import SimpleNamespace

import pytest

from hypeman_social.llm.ollama import OllamaLLM


@pytest.fixture
def llm():
    return OllamaLLM()


# ─────────────────────────────────────────────────────────────────────────────
# Response unpacking
# ─────────────────────────────────────────────────────────────────────────────

def test_unpacks_a_dict_response(llm):
    content, thinking = llm._unpack({'response': '  a post  ', 'thinking': 'reasoning'})
    assert content == 'a post'
    assert thinking == 'reasoning'


def test_unpacks_a_pydantic_style_response(llm):
    response = SimpleNamespace(response='a post', thinking='reasoning')
    assert llm._unpack(response) == ('a post', 'reasoning')


def test_unpacks_chat_style_nested_message(llm):
    """chat() nests content and thinking under `message`; generate() doesn't."""
    content, thinking = llm._unpack({
        'response': '',
        'message': {'content': 'a post', 'thinking': 'reasoning'},
    })
    assert content == 'a post'
    assert thinking == 'reasoning'


def test_unpacks_a_bare_string(llm):
    assert llm._unpack('just text') == ('just text', '')


def test_missing_thinking_field_is_empty_not_none(llm):
    """Callers do truthiness checks on this; None would work, '' is tidier."""
    _, thinking = llm._unpack({'response': 'a post'})
    assert thinking == ''


# ─────────────────────────────────────────────────────────────────────────────
# Model listing across client versions
# ─────────────────────────────────────────────────────────────────────────────

def test_model_names_from_attribute_style_response():
    response = SimpleNamespace(models=[{'name': 'gemma3:4b'}, {'model': 'qwen3:8b'}])
    assert OllamaLLM._model_names(response) == ['gemma3:4b', 'qwen3:8b']


def test_model_names_from_dict_style_response():
    response = {'models': [{'name': 'gemma3:4b'}]}
    assert OllamaLLM._model_names(response) == ['gemma3:4b']


def test_model_names_from_object_entries():
    """Newer clients return objects rather than dicts."""
    response = SimpleNamespace(models=[SimpleNamespace(model='gemma3:4b', name=None)])
    assert OllamaLLM._model_names(response) == ['gemma3:4b']


def test_model_names_handles_an_empty_response():
    assert OllamaLLM._model_names({}) == []
    assert OllamaLLM._model_names(SimpleNamespace(models=[])) == []


# ─────────────────────────────────────────────────────────────────────────────
# Thinking-mode fallback
# ─────────────────────────────────────────────────────────────────────────────

def test_empty_content_falls_back_to_thinking(monkeypatch):
    """
    Reasoning models sometimes burn the whole budget thinking.

    The post is usually still in the thinking text, so salvage it rather than
    dropping to a template.
    """
    monkeypatch.setenv('LLM_ENABLE_THINKING_MODE', 'true')

    llm = OllamaLLM()
    llm.client = SimpleNamespace(generate=lambda **kw: {
        'response': '',
        'thinking': "Let me draft this.\n> Firmware teardown tonight, come watch. #Hardware",
    })

    result = llm._raw_generate("prompt", 150)
    assert result is not None
    assert "Firmware teardown tonight" in result


def test_thinking_fallback_is_off_when_disabled(monkeypatch):
    monkeypatch.setenv('LLM_ENABLE_THINKING_MODE', 'false')

    llm = OllamaLLM()
    llm.client = SimpleNamespace(generate=lambda **kw: {
        'response': '',
        'thinking': "> Firmware teardown tonight, come watch. #Hardware",
    })

    assert llm._raw_generate("prompt", 150) == ''


def test_generating_without_a_client_raises_a_connection_error(llm):
    """
    Must raise, not return None.

    BaseLLM.generate classifies the exception to decide whether to reconnect;
    swallowing it here would hide a downed server.
    """
    with pytest.raises(ConnectionError):
        llm._raw_generate("prompt", 150)


# ─────────────────────────────────────────────────────────────────────────────
# Explicit think control for thinking-capable models
# ─────────────────────────────────────────────────────────────────────────────

def _capture_generate(calls):
    def generate(**kwargs):
        calls.append(kwargs)
        return {'response': 'a post', 'thinking': ''}
    return generate


def test_think_false_sent_when_model_thinks_but_mode_is_off(monkeypatch):
    """
    Hybrid reasoning models (gemma4, qwen3) think by default, and generate()
    withholds the reasoning — the whole num_predict budget goes to thoughts we
    never see and the content comes back empty. With thinking mode off, the
    request must say think=False so the model answers directly.
    """
    monkeypatch.setenv('LLM_ENABLE_THINKING_MODE', 'false')

    calls = []
    llm = OllamaLLM()
    llm._supports_thinking = True
    llm.client = SimpleNamespace(generate=_capture_generate(calls))

    llm._raw_generate("prompt", 150)
    assert calls[0]['think'] is False


def test_think_true_sent_when_thinking_mode_is_on(monkeypatch):
    monkeypatch.setenv('LLM_ENABLE_THINKING_MODE', 'true')

    calls = []
    llm = OllamaLLM()
    llm._supports_thinking = True
    llm.client = SimpleNamespace(generate=_capture_generate(calls))

    llm._raw_generate("prompt", 150)
    assert calls[0]['think'] is True


def test_think_not_sent_for_non_thinking_models():
    """
    Old Ollama servers predate the think parameter; models without the
    capability must never see it.
    """
    calls = []
    llm = OllamaLLM()
    llm._supports_thinking = False
    llm.client = SimpleNamespace(generate=_capture_generate(calls))

    llm._raw_generate("prompt", 150)
    assert 'think' not in calls[0]


def test_detects_thinking_capability_from_show():
    llm = OllamaLLM()
    llm.model = 'gemma4:12b'

    client = SimpleNamespace(
        show=lambda model: {'capabilities': ['completion', 'thinking']})
    assert llm._detect_thinking_support(client) is True

    client = SimpleNamespace(show=lambda model: {'capabilities': ['completion']})
    assert llm._detect_thinking_support(client) is False


def test_thinking_detection_failure_means_no_think_param():
    """A server without /api/show capabilities just gets the old behaviour."""
    def boom(model):
        raise RuntimeError("404")

    llm = OllamaLLM()
    llm.model = 'gemma3:4b'
    assert llm._detect_thinking_support(SimpleNamespace(show=boom)) is False


# ─────────────────────────────────────────────────────────────────────────────
# Default model, num_ctx, keep_alive
# ─────────────────────────────────────────────────────────────────────────────

def _fake_ollama_module(monkeypatch):
    """Patch the ollama module with a fake Client; no network, no server."""
    import hypeman_social.llm.ollama as mod

    class FakeClient:
        def __init__(self, host=None):
            self.host = host

        def list(self):
            return {'models': []}

        def show(self, model):
            return {'capabilities': ['completion']}

    monkeypatch.setattr(mod, 'ollama', SimpleNamespace(Client=FakeClient), raising=False)
    monkeypatch.setattr(mod, 'OLLAMA_AVAILABLE', True)


def _clear_ollama_env(monkeypatch):
    for key in ('LLM_MODEL', 'LLM_OLLAMA_MODEL', 'LLM_NUM_CTX', 'LLM_OLLAMA_NUM_CTX',
                'LLM_KEEP_ALIVE', 'LLM_OLLAMA_KEEP_ALIVE'):
        monkeypatch.delenv(key, raising=False)


def test_default_model_is_gemma4_12b(monkeypatch):
    _fake_ollama_module(monkeypatch)
    _clear_ollama_env(monkeypatch)
    llm = OllamaLLM()
    llm.authenticate()
    assert llm.model == 'gemma4:12b'


def test_model_can_be_pinned_to_the_old_default(monkeypatch):
    _fake_ollama_module(monkeypatch)
    _clear_ollama_env(monkeypatch)
    monkeypatch.setenv('LLM_OLLAMA_MODEL', 'gemma3:4b')
    llm = OllamaLLM()
    llm.authenticate()
    assert llm.model == 'gemma3:4b'


def test_num_ctx_and_keep_alive_not_sent_when_unset(monkeypatch):
    _fake_ollama_module(monkeypatch)
    _clear_ollama_env(monkeypatch)
    calls = []
    llm = OllamaLLM()
    llm.authenticate()
    llm.client = SimpleNamespace(generate=_capture_generate(calls))

    llm._raw_generate("prompt", 150)
    assert 'num_ctx' not in calls[0]['options']
    assert 'keep_alive' not in calls[0]


def test_num_ctx_sent_exactly_as_configured(monkeypatch):
    _fake_ollama_module(monkeypatch)
    _clear_ollama_env(monkeypatch)
    monkeypatch.setenv('LLM_OLLAMA_NUM_CTX', '8192')
    calls = []
    llm = OllamaLLM()
    llm.authenticate()
    llm.client = SimpleNamespace(generate=_capture_generate(calls))

    llm._raw_generate("prompt", 150)
    assert calls[0]['options']['num_ctx'] == 8192


def test_keep_alive_passed_through_when_set(monkeypatch):
    _fake_ollama_module(monkeypatch)
    _clear_ollama_env(monkeypatch)
    monkeypatch.setenv('LLM_OLLAMA_KEEP_ALIVE', '30s')
    calls = []
    llm = OllamaLLM()
    llm.authenticate()
    llm.client = SimpleNamespace(generate=_capture_generate(calls))

    llm._raw_generate("prompt", 150)
    assert calls[0]['keep_alive'] == '30s'


def test_invalid_num_ctx_is_not_sent(monkeypatch):
    """A typo must not become a guessed number; it is dropped with a warning."""
    _fake_ollama_module(monkeypatch)
    _clear_ollama_env(monkeypatch)
    monkeypatch.setenv('LLM_OLLAMA_NUM_CTX', 'lots')
    calls = []
    llm = OllamaLLM()
    llm.authenticate()
    llm.client = SimpleNamespace(generate=_capture_generate(calls))

    llm._raw_generate("prompt", 150)
    assert 'num_ctx' not in calls[0]['options']
