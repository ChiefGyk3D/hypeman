# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Claude and OpenAI providers, exercised against fake SDK clients.

No network and no real SDK calls: the module-level SDK handle is replaced
with a fake whose client records requests and replays scripted responses.
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hypeman_social.llm import claude as claude_mod
from hypeman_social.llm import openai_llm as openai_mod
from hypeman_social.llm._cloud import RateLimiter, classify_sdk_error
from hypeman_social.llm.claude import ClaudeLLM
from hypeman_social.llm.manager import LLMManager, build_provider
from hypeman_social.llm.openai_llm import OpenAILLM
from hypeman_social.llm.profiles import GENERIC_PROFILE

FAKE_KEY = 'sk-test-FAKEKEY-do-not-log-0123456789'


# ─────────────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────────────

class RateLimitError(Exception):
    status_code = 429


class AuthenticationError(Exception):
    status_code = 401


class BadRequestError(Exception):
    status_code = 400


class APIConnectionError(Exception):
    pass


class APITimeoutError(APIConnectionError):
    pass


class InternalServerError(Exception):
    status_code = 500


class Script:
    """Replays results in order; exceptions are raised. Records every request."""

    def __init__(self, results):
        self.results = list(results)
        self.requests = []

    def next(self, kwargs):
        self.requests.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def make_sdk(factory_name, attr, script):
    """A fake SDK module: <factory_name>(**kw) -> client exposing <attr>.create."""
    created = []

    def factory(**kwargs):
        created.append(kwargs)
        namespace = SimpleNamespace(create=lambda **kw: script.next(kw))
        client = SimpleNamespace(**{attr: namespace})
        return client

    return SimpleNamespace(**{factory_name: factory}), created


def claude_reply(text='hello there', stop_reason='end_turn'):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type='text', text=text)] if text else [],
    )


def openai_reply(text='hello there', status='completed', refusal=None):
    parts = [SimpleNamespace(type='refusal', refusal=refusal)] if refusal else []
    return SimpleNamespace(
        status=status,
        output=[SimpleNamespace(type='message', content=parts)],
        output_text=text,
    )


@pytest.fixture(autouse=True)
def _no_sleep_and_clean_env(monkeypatch):
    for var in ('CLAUDE_EFFORT', 'CLAUDE_EFFORT_LEVEL', 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'LLM_CLAUDE_API_KEY',
                'LLM_OPENAI_API_KEY', 'LLM_MODEL', 'DOPPLER_TOKEN'):
        monkeypatch.delenv(var, raising=False)
    with patch('hypeman_social.llm.base.time.sleep'):
        yield


@pytest.fixture
def claude(monkeypatch):
    script = Script([claude_reply()])
    sdk, created = make_sdk('Anthropic', 'messages', script)
    monkeypatch.setattr(claude_mod, 'anthropic', sdk)
    monkeypatch.setattr(claude_mod, 'CLAUDE_AVAILABLE', True)
    monkeypatch.setenv('ANTHROPIC_API_KEY', FAKE_KEY)
    llm = ClaudeLLM()
    assert llm.authenticate() is True
    llm.script, llm.created = script, created
    llm.limiter = RateLimiter(0)  # real spacing is covered by its own tests
    return llm


@pytest.fixture
def oai(monkeypatch):
    script = Script([openai_reply()])
    sdk, created = make_sdk('OpenAI', 'responses', script)
    monkeypatch.setattr(openai_mod, 'openai', sdk)
    monkeypatch.setattr(openai_mod, 'OPENAI_AVAILABLE', True)
    monkeypatch.setenv('OPENAI_API_KEY', FAKE_KEY)
    llm = OpenAILLM()
    assert llm.authenticate() is True
    llm.script, llm.created = script, created
    llm.limiter = RateLimiter(0)  # real spacing is covered by its own tests
    return llm


# ─────────────────────────────────────────────────────────────────────────────
# Claude
# ─────────────────────────────────────────────────────────────────────────────

def test_claude_generate_happy_path(claude):
    assert claude.generate('say hi') == 'hello there'
    request = claude.script.requests[0]
    assert request['model'] == 'claude-haiku-5-5'
    assert request['messages'] == [{'role': 'user', 'content': 'say hi'}]
    assert request['max_tokens'] == claude.max_tokens


def test_claude_client_has_timeout_and_no_sdk_retries(claude):
    assert claude.created[0]['timeout'] == 30.0
    assert claude.created[0]['max_retries'] == 0


def test_claude_never_sends_sampling_parameters(claude):
    """Haiku/Sonnet 5.5 400 on non-default temperature; the base default is 0.3."""
    claude.generate('x')
    request = claude.script.requests[0]
    assert not {'temperature', 'top_p', 'top_k'} & set(request)


def test_claude_model_is_configurable(monkeypatch, claude):
    monkeypatch.setenv('LLM_CLAUDE_MODEL', 'claude-sonnet-5-5')
    assert claude.provider_config('model') == 'claude-sonnet-5-5'


def test_claude_system_block_carries_cache_control(claude):
    claude.set_system_prompt('You are the persona. Rules follow.')
    claude.generate('x')
    system = claude.script.requests[0]['system']
    assert system == [{
        'type': 'text',
        'text': 'You are the persona. Rules follow.',
        'cache_control': {'type': 'ephemeral'},
    }]


def test_claude_prompt_cache_can_be_disabled(claude):
    claude.set_system_prompt('persona')
    claude.enable_prompt_cache = False
    claude.generate('x')
    assert 'cache_control' not in claude.script.requests[0]['system'][0]


def test_claude_cache_ttl_one_hour(claude):
    claude.set_system_prompt('persona')
    claude.cache_ttl = '1h'
    claude.generate('x')
    assert claude.script.requests[0]['system'][0]['cache_control'] == {
        'type': 'ephemeral', 'ttl': '1h'}


def test_claude_without_system_prompt_sends_no_system(claude):
    claude.generate('x')
    assert 'system' not in claude.script.requests[0]


def test_claude_defaults_to_low_effort_and_no_thinking_on_haiku(claude):
    claude.generate('x')
    request = claude.script.requests[0]
    assert request['output_config'] == {'effort': 'low'}
    assert request['thinking'] == {'type': 'disabled'}


def test_claude_thinking_not_disabled_on_other_models(claude):
    """Sonnet/Opus 5.5 reject thinking disabled; auto must omit it there."""
    claude.model = 'claude-sonnet-5-5'
    claude.generate('x')
    assert 'thinking' not in claude.script.requests[0]


def test_claude_effort_can_be_omitted(claude):
    claude.effort = 'omit'
    claude.generate('x')
    assert 'output_config' not in claude.script.requests[0]


def test_claude_refusal_stop_reason_returns_none(claude):
    claude.script.results = [claude_reply(text='', stop_reason='refusal')]
    assert claude.generate('x') is None


def test_claude_joins_text_blocks_and_skips_others(claude):
    claude.script.results = [SimpleNamespace(stop_reason='end_turn', content=[
        SimpleNamespace(type='thinking', thinking=''),
        SimpleNamespace(type='text', text='  one '),
        SimpleNamespace(type='text', text='two  '),
    ])]
    assert claude.generate('x') == 'one two'


def test_claude_retries_a_rate_limit_then_succeeds(claude):
    claude.script.results = [RateLimitError('slow down'), claude_reply('ok')]
    assert claude.generate('x', max_retries=3) == 'ok'
    assert len(claude.script.requests) == 2


def test_claude_retries_overload_and_timeout(claude):
    claude.script.results = [InternalServerError('x'), APITimeoutError('x'), claude_reply('ok')]
    assert claude.generate('x', max_retries=3) == 'ok'
    assert claude.enabled is True, 'a timeout must not mark the provider down'


def test_claude_permanent_errors_are_not_retried(claude):
    claude.script.results = [BadRequestError('x'), claude_reply('never')]
    assert claude.generate('x', max_retries=3) is None
    assert len(claude.script.requests) == 1


def test_claude_auth_error_is_permanent(claude):
    claude.script.results = [AuthenticationError('x'), claude_reply('never')]
    assert claude.generate('x', max_retries=3) is None
    assert len(claude.script.requests) == 1


def test_claude_connection_error_marks_down_and_heals(claude):
    claude.script.results = [APIConnectionError('x')]
    claude.reconnect_interval = 0
    claude.max_retries = 1
    assert claude.generate('x') is None
    # After a reconnect the provider is usable again (stateless HTTP).
    claude.script.results = [claude_reply('back')]
    assert claude.is_available() is True
    assert claude.generate('x') == 'back'


def test_claude_limiter_spaces_calls(claude):
    sleeps = []
    clock = iter([0.0, 0.1, 2.0])
    claude.limiter = RateLimiter(
        30, clock=lambda: next(clock), sleep=sleeps.append)
    claude.script.results = [claude_reply(), claude_reply()]
    claude.generate('a')
    claude.generate('b')
    assert sleeps == [pytest.approx(1.9)]


def test_claude_missing_extra_fails_closed(monkeypatch):
    monkeypatch.setattr(claude_mod, 'CLAUDE_AVAILABLE', False)
    monkeypatch.setenv('ANTHROPIC_API_KEY', FAKE_KEY)
    assert ClaudeLLM().authenticate() is False


def test_claude_no_key_fails_but_keeps_nothing_enabled(monkeypatch):
    monkeypatch.setattr(claude_mod, 'CLAUDE_AVAILABLE', True)
    llm = ClaudeLLM()
    assert llm.authenticate() is False
    assert llm.enabled is False


def test_claude_accepts_prefixed_key(monkeypatch):
    script = Script([])
    sdk, created = make_sdk('Anthropic', 'messages', script)
    monkeypatch.setattr(claude_mod, 'anthropic', sdk)
    monkeypatch.setattr(claude_mod, 'CLAUDE_AVAILABLE', True)
    monkeypatch.setenv('LLM_CLAUDE_API_KEY', FAKE_KEY)
    assert ClaudeLLM().authenticate() is True
    assert created[0]['api_key'] == FAKE_KEY


# ─────────────────────────────────────────────────────────────────────────────
# OpenAI
# ─────────────────────────────────────────────────────────────────────────────

VERDICT_SCHEMA = {
    'type': 'object',
    'properties': {'verdict': {'type': 'string'}, 'reason': {'type': 'string'}},
    'required': ['verdict', 'reason'],
    'additionalProperties': False,
}


def test_openai_generate_happy_path(oai):
    assert oai.generate('say hi') == 'hello there'
    request = oai.script.requests[0]
    assert request['model'] == 'gpt-6-luna'
    assert request['input'] == 'say hi'
    assert request['max_output_tokens'] == oai.max_tokens
    assert request['reasoning'] == {'effort': 'low'}
    assert request['store'] is False
    assert 'text' not in request
    assert not {'temperature', 'top_p'} & set(request)


def test_openai_client_has_timeout_and_no_sdk_retries(oai):
    assert oai.created[0]['timeout'] == 30.0
    assert oai.created[0]['max_retries'] == 0
    assert 'base_url' not in oai.created[0]


def test_openai_system_prompt_goes_in_instructions(oai):
    oai.set_system_prompt('persona')
    oai.generate('x')
    assert oai.script.requests[0]['instructions'] == 'persona'


def test_openai_reasoning_effort_can_be_omitted(oai):
    oai.reasoning_effort = 'omit'
    oai.generate('x')
    assert 'reasoning' not in oai.script.requests[0]


def test_openai_structured_happy_path(oai):
    verdict = {'verdict': 'safe', 'reason': 'ok'}
    oai.script.results = [openai_reply(json.dumps(verdict))]
    assert oai.generate_structured('classify', VERDICT_SCHEMA, name='verdict') == verdict
    fmt = oai.script.requests[0]['text']['format']
    assert fmt == {
        'type': 'json_schema', 'name': 'verdict', 'strict': True, 'schema': VERDICT_SCHEMA}


def test_openai_structured_schema_does_not_leak_into_next_call(oai):
    oai.script.results = [openai_reply('{"verdict":"a","reason":"b"}'), openai_reply('plain')]
    oai.generate_structured('c', VERDICT_SCHEMA)
    assert oai.generate('d') == 'plain'
    assert 'text' not in oai.script.requests[1]


@pytest.mark.parametrize('reply', [
    openai_reply('not json at all'),
    openai_reply('["a list"]'),
    openai_reply('{"verdict": "safe"}'),
    openai_reply('{"verdict":"safe","reason":"x"}', status='incomplete'),
    openai_reply('', refusal='cannot help'),
    openai_reply('{"verdict":"safe","reason":"x"}', refusal='cannot help'),
    openai_reply(''),
])
def test_openai_structured_failure_is_a_refusal_never_a_guess(oai, reply):
    oai.script.results = [reply]
    assert oai.generate_structured('classify', VERDICT_SCHEMA) is None


def test_openai_refusal_on_free_text_returns_none(oai):
    oai.script.results = [openai_reply('', refusal='no')]
    assert oai.generate('x') is None


def test_openai_structured_still_retries_transient_errors(oai):
    oai.script.results = [RateLimitError('x'), openai_reply('{"verdict":"a","reason":"b"}')]
    assert oai.generate_structured('c', VERDICT_SCHEMA) == {'verdict': 'a', 'reason': 'b'}
    assert len(oai.script.requests) == 2


def test_openai_retry_and_permanent_classification(oai):
    oai.script.results = [InternalServerError('x'), openai_reply('ok')]
    assert oai.generate('x', max_retries=3) == 'ok'
    oai.script.results = [BadRequestError('x'), openai_reply('never')]
    oai.script.requests.clear()
    assert oai.generate('x', max_retries=3) is None
    assert len(oai.script.requests) == 1


def test_openai_base_url_only_when_configured(monkeypatch):
    script = Script([])
    sdk, created = make_sdk('OpenAI', 'responses', script)
    monkeypatch.setattr(openai_mod, 'openai', sdk)
    monkeypatch.setattr(openai_mod, 'OPENAI_AVAILABLE', True)
    monkeypatch.setenv('OPENAI_API_KEY', FAKE_KEY)
    monkeypatch.setenv('LLM_OPENAI_BASE_URL', 'https://gateway.example/v1')
    assert OpenAILLM().authenticate() is True
    assert created[0]['base_url'] == 'https://gateway.example/v1'


def test_openai_limiter_default_and_disable(oai):
    assert OpenAILLM().rate_limit == 60
    assert ClaudeLLM().rate_limit == 50
    RateLimiter(0).wait()  # disabled: must not block or raise


def test_openai_missing_extra_fails_closed(monkeypatch):
    monkeypatch.setattr(openai_mod, 'OPENAI_AVAILABLE', False)
    monkeypatch.setenv('OPENAI_API_KEY', FAKE_KEY)
    assert OpenAILLM().authenticate() is False


# ─────────────────────────────────────────────────────────────────────────────
# Secrets never reach logs or exceptions
# ─────────────────────────────────────────────────────────────────────────────

class LeakyError(Exception):
    """An SDK error whose text echoes the credential."""

    status_code = 401

    def __init__(self):
        super().__init__(f'Incorrect API key provided: {FAKE_KEY}')


@pytest.mark.parametrize('fixture_name', ['claude', 'oai'])
def test_key_never_appears_in_logs_status_or_errors(request, caplog, fixture_name):
    llm = request.getfixturevalue(fixture_name)
    llm.script.results = [LeakyError()]
    caplog.set_level(logging.DEBUG)

    assert llm.generate('x', max_retries=2) is None
    with pytest.raises(RuntimeError) as excinfo:
        llm.script.results = [LeakyError()]
        llm._raw_generate('x', 10)

    assert FAKE_KEY not in caplog.text
    assert FAKE_KEY not in str(excinfo.value)
    assert FAKE_KEY not in repr(excinfo.value.__cause__)
    assert FAKE_KEY not in json.dumps(llm.status(), default=str)


def test_key_not_logged_at_authenticate(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    script = Script([])
    for mod, flag, name, attr, cls, env in (
        (claude_mod, 'CLAUDE_AVAILABLE', 'anthropic', 'Anthropic', ClaudeLLM, 'ANTHROPIC_API_KEY'),
        (openai_mod, 'OPENAI_AVAILABLE', 'openai', 'OpenAI', OpenAILLM, 'OPENAI_API_KEY'),
    ):
        sdk, _ = make_sdk(attr, 'messages', script)
        monkeypatch.setattr(mod, name, sdk)
        monkeypatch.setattr(mod, flag, True)
        monkeypatch.setenv(env, FAKE_KEY)
        assert cls().authenticate() is True
    assert FAKE_KEY not in caplog.text


# ─────────────────────────────────────────────────────────────────────────────
# Error classification
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('error, expected', [
    (RateLimitError(), 'rate limit'),
    (AuthenticationError(), 'unauthorized'),
    (BadRequestError(), 'invalid'),
    (APITimeoutError(), 'timed out'),
    (APIConnectionError(), 'connection'),
    (InternalServerError(), 'unavailable'),
])
def test_classify_sdk_error(error, expected):
    assert expected in classify_sdk_error(error)


# ─────────────────────────────────────────────────────────────────────────────
# Registration and failover
# ─────────────────────────────────────────────────────────────────────────────

def test_build_provider_registers_both_and_the_alias():
    assert isinstance(build_provider('claude', GENERIC_PROFILE), ClaudeLLM)
    assert isinstance(build_provider('anthropic', GENERIC_PROFILE), ClaudeLLM)
    assert isinstance(build_provider('OpenAI', GENERIC_PROFILE), OpenAILLM)
    assert build_provider('nope', GENERIC_PROFILE) is None


def test_fallback_chain_reaches_a_cloud_provider(claude):
    from tests.test_availability import FakeLLM
    manager = LLMManager()
    manager.primary = FakeLLM(server_up=False)
    manager.primary.provider_name = 'primary'
    manager.fallbacks = [claude]
    manager.enabled = True
    manager.primary.mark_unavailable()
    claude.script.results = [claude_reply('from claude')]

    assert manager.is_available() is True
    assert manager.generate('x') == 'from claude'
    assert manager.using_fallback is True


def test_fallback_is_still_opt_in(monkeypatch):
    monkeypatch.delenv('LLM_FALLBACK_PROVIDER', raising=False)
    assert LLMManager._fallback_names('ollama') == []
    monkeypatch.setenv('LLM_FALLBACK_PROVIDER', 'claude,openai')
    assert LLMManager._fallback_names('ollama') == ['claude', 'openai']
