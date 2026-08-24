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
