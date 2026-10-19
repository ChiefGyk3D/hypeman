# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Claude and OpenAI providers, exercised against fake SDK clients.

No network and no real SDK calls: the module-level SDK handle is replaced
with a fake whose client records requests and replays scripted responses.
"""

import asyncio
import json
import logging
import threading
import traceback
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

    def http_client(**kwargs):  # fake of the SDK's DefaultHttpxClient
        return SimpleNamespace(**kwargs)

    return SimpleNamespace(**{factory_name: factory, 'DefaultHttpxClient': http_client}), created


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
                'LLM_OPENAI_API_KEY', 'LLM_MODEL', 'DOPPLER_TOKEN', 'OPENAI_BASE_URL', 'OPENAI_MODEL',
                'CLAUDE_MODEL', 'LLM_OPENAI_BASE_URL', 'LLM_OPENAI_MODEL', 'LLM_CLAUDE_MODEL',
                'LLM_OPENAI_RATE_LIMIT', 'LLM_CLAUDE_RATE_LIMIT', 'LLM_OPENAI_TIMEOUT',
                'LLM_CLAUDE_TIMEOUT', 'LLM_FALLBACK_PROVIDER', 'OPENAI_TIMEOUT', 'CLAUDE_TIMEOUT'):
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
    # The refusal carries text on purpose: only the stop_reason check can
    # turn this into None, so deleting that check fails the test.
    claude.script.results = [claude_reply(text='partial answer', stop_reason='refusal')]
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
    claude.script.results = [APIConnectionError('x'), claude_reply('never')]
    claude.enable_auto_reconnect = False  # hold the provider down so we can see it
    assert claude.generate('x', max_retries=3) is None
    # Classified as a connection loss: down, no retry, reason recorded.
    assert claude.enabled is False
    assert len(claude.script.requests) == 1
    assert 'connection' in claude.status()['last_error']
    assert claude.is_available() is False

    # Reconnect is allowed again: the stateless client heals and serves.
    claude.enable_auto_reconnect = True
    claude.reconnect_interval = 0
    claude.script.results = [claude_reply('back')]
    assert claude.is_available() is True
    assert claude.enabled is True
    assert claude.generate('x') == 'back'
    assert len(claude.script.requests) == 2


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
    assert oai.created[0]['base_url'] == 'https://api.openai.com/v1'


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


def test_openai_base_url_when_configured(monkeypatch):
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
    llm.script.results = [LeakyError()]
    with pytest.raises(RuntimeError) as excinfo:
        llm._raw_generate('x', 10)
    error = excinfo.value

    assert FAKE_KEY not in caplog.text
    assert FAKE_KEY not in str(error)
    # `from None` only hides __cause__; the implicit __context__ must be gone too.
    assert error.__cause__ is None
    assert error.__context__ is None
    formatted = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
    assert FAKE_KEY not in formatted
    assert 'LeakyError' not in formatted
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


# ─────────────────────────────────────────────────────────────────────────────
# Fix round: endpoint/config hijack, schema validation, bad config, aliases
# ─────────────────────────────────────────────────────────────────────────────

def _openai_sdk(monkeypatch):
    script = Script([])
    sdk, created = make_sdk('OpenAI', 'responses', script)
    monkeypatch.setattr(openai_mod, 'openai', sdk)
    monkeypatch.setattr(openai_mod, 'OPENAI_AVAILABLE', True)
    monkeypatch.setenv('OPENAI_API_KEY', FAKE_KEY)
    return created


def test_ambient_bare_endpoint_cannot_redirect_the_key(monkeypatch):
    created = _openai_sdk(monkeypatch)
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://attacker.invalid/v1')
    monkeypatch.setenv('LLM_OPENAI_BASE_URL', 'https://gateway.example/v1')
    assert OpenAILLM().authenticate() is True
    assert created[0]['base_url'] == 'https://gateway.example/v1'


def test_ambient_bare_endpoint_alone_is_ignored(monkeypatch):
    created = _openai_sdk(monkeypatch)
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://attacker.invalid/v1')
    assert OpenAILLM().authenticate() is True
    assert created[0]['base_url'] == 'https://api.openai.com/v1'


def test_ambient_bare_model_and_settings_do_not_win(monkeypatch, claude):
    monkeypatch.setenv('OPENAI_MODEL', 'attacker-model')
    monkeypatch.setenv('LLM_OPENAI_MODEL', 'real-model')
    monkeypatch.setenv('CLAUDE_MODEL', 'attacker-model')
    monkeypatch.setenv('LLM_CLAUDE_MODEL', 'claude-sonnet-5-5')
    monkeypatch.setenv('CLAUDE_EFFORT_LEVEL', 'max')
    monkeypatch.setenv('CLAUDE_TIMEOUT', '1')
    monkeypatch.setenv('OPENAI_TIMEOUT', '1')
    created = _openai_sdk(monkeypatch)
    llm = OpenAILLM()
    assert llm.authenticate() is True
    assert llm.model == 'real-model'
    assert llm.timeout == 30.0
    assert created[0]['timeout'] == 30.0
    fresh = ClaudeLLM()
    assert fresh.authenticate() is True
    assert fresh.model == 'claude-sonnet-5-5'
    assert fresh.effort == 'low'
    assert fresh.timeout == 30.0


@pytest.mark.parametrize('url', [
    'http://gateway.example/v1',          # plaintext to a remote host
    'ftp://gateway.example/v1',
    'https://user:pw@gateway.example/v1',  # userinfo
    'https://@gateway.example/v1',
    'https:///v1',                         # no host
    'gateway.example/v1',                  # no scheme
    'https://gateway.example/v1?x=1',
    'https://gateway.example/v1#frag',
    'https://gateway.example/\nv1',
    'javascript:alert(1)',
    'https://[::1',                        # unparseable
    'https://gateway.example:99999/v1',    # port out of range
    'https://gateway.example:abc/v1',      # non-numeric port
])
def test_rejected_base_url_never_gets_the_credential(monkeypatch, caplog, url):
    created = _openai_sdk(monkeypatch)
    monkeypatch.setenv('LLM_OPENAI_BASE_URL', url)
    caplog.set_level(logging.DEBUG)
    llm = OpenAILLM()
    assert llm.authenticate() is False
    assert created == [], 'no client (and so no credential) for a rejected endpoint'
    assert llm.client is None
    assert llm._reconnect() is False
    assert llm.is_available() is False
    assert created == []
    assert FAKE_KEY not in caplog.text
    assert url not in caplog.text


@pytest.mark.parametrize('url', [
    'http://localhost:11434/v1', 'http://127.0.0.1:8080/v1', 'http://[::1]:8080/v1',
    'https://gateway.example/v1',
])
def test_loopback_http_and_https_urls_are_accepted(monkeypatch, url):
    created = _openai_sdk(monkeypatch)
    monkeypatch.setenv('LLM_OPENAI_BASE_URL', url)
    assert OpenAILLM().authenticate() is True
    assert created[0]['base_url'] == url


# --- structured output is validated against the caller's schema ---------------

BOOL_SCHEMA = {
    'type': 'object',
    'properties': {'verdict': {'type': 'boolean'}},
    'required': ['verdict'],
    'additionalProperties': False,
}


@pytest.mark.parametrize('raw', [
    '{"verdict":"false","extra":1}',        # wrong type and an extra key
    '{"verdict":"false"}',                  # wrong type
    '{"verdict":false,"extra":1}',          # additionalProperties false
    '{"verdict":false,"verdict":true}',     # duplicate key
    '{"verdict":NaN}',
    '{"verdict":Infinity}',
    '{"verdict":1}',                        # int is not a boolean
    '{}',
])
def test_structured_output_must_match_the_schema_exactly(oai, raw):
    oai.script.results = [openai_reply(raw)]
    assert oai.generate_structured('c', BOOL_SCHEMA) is None


def test_structured_output_that_matches_is_returned(oai):
    oai.script.results = [openai_reply('{"verdict":false}')]
    assert oai.generate_structured('c', BOOL_SCHEMA) == {'verdict': False}


def test_structured_validator_covers_the_supported_subset(oai):
    schema = {
        'type': 'object',
        'properties': {
            'label': {'type': 'string', 'enum': ['a', 'b']},
            'score': {'type': 'number'},
            'count': {'type': 'integer'},
            'tags': {'type': 'array', 'items': {'type': 'string'}},
            'note': {'type': 'null'},
        },
        'required': ['label', 'score'],
        'additionalProperties': False,
    }
    good = '{"label":"a","score":1.5,"count":2,"tags":["x"],"note":null}'
    oai.script.results = [openai_reply(good)]
    assert oai.generate_structured('c', schema)['label'] == 'a'
    for bad in (
        '{"label":"c","score":1}',              # enum
        '{"label":"a","score":true}',           # bool is not a number
        '{"label":"a","score":1,"count":1.5}',  # not an integer
        '{"label":"a","score":1,"tags":[1]}',   # items
        '{"label":"a","score":1,"note":0}',     # null
        '{"label":"a"}',                        # required
        '[1]',
    ):
        oai.script.results = [openai_reply(bad)]
        assert oai.generate_structured('c', schema) is None, bad


def test_unsupported_schema_keyword_is_rejected_not_ignored(oai):
    schema = dict(BOOL_SCHEMA, properties={'verdict': {'type': 'string', 'pattern': '^a$'}})
    oai.script.results = [openai_reply('{"verdict":"zzz"}')]
    assert oai.generate_structured('c', schema) is None
    assert oai.script.requests == [], 'an unverifiable schema must not even be sent'


# --- invalid configuration degrades instead of raising ------------------------

@pytest.mark.parametrize('var', ['LLM_OPENAI_RATE_LIMIT', 'LLM_OPENAI_TIMEOUT'])
@pytest.mark.parametrize('bad', ['oops', 'nan', 'inf', '-5', '0x10', ''])
def test_bad_openai_numeric_config_uses_defaults(monkeypatch, caplog, var, bad):
    monkeypatch.setenv(var, bad)
    caplog.set_level(logging.WARNING)
    llm = OpenAILLM()  # must not raise
    assert llm.timeout == 30.0
    assert llm.rate_limit == 60
    if bad:
        assert var in caplog.text
        assert bad not in caplog.text or bad.isdigit()


@pytest.mark.parametrize('var', ['LLM_CLAUDE_RATE_LIMIT', 'LLM_CLAUDE_TIMEOUT'])
@pytest.mark.parametrize('bad', ['oops', 'nan', 'inf', '-5'])
def test_bad_claude_numeric_config_uses_defaults(monkeypatch, caplog, var, bad):
    monkeypatch.setenv(var, bad)
    caplog.set_level(logging.WARNING)
    llm = ClaudeLLM()
    assert llm.timeout == 30.0
    assert llm.rate_limit == 50
    assert var in caplog.text
    assert bad not in caplog.text


def test_zero_rate_limit_still_disables_throttling(monkeypatch):
    monkeypatch.setenv('LLM_CLAUDE_RATE_LIMIT', '0')
    assert ClaudeLLM().rate_limit == 0


def test_manager_survives_bad_provider_config(monkeypatch):
    monkeypatch.setenv('LLM_ENABLE', 'true')
    monkeypatch.setenv('LLM_PROVIDER', 'ollama')
    monkeypatch.setenv('LLM_FALLBACK_PROVIDER', 'openai,claude')
    monkeypatch.setenv('LLM_OPENAI_RATE_LIMIT', 'oops')
    monkeypatch.setenv('LLM_CLAUDE_TIMEOUT', 'oops')
    manager = LLMManager()
    assert manager.authenticate() is True
    assert [p.provider_name for p in manager.fallbacks] == ['openai', 'claude']


# --- sanitized errors carry no credential-bearing context ---------------------

@pytest.mark.parametrize('fixture_name', ['claude', 'oai'])
def test_sanitized_error_has_no_context_or_cause(request, fixture_name):
    llm = request.getfixturevalue(fixture_name)
    llm.script.results = [LeakyError()]
    with pytest.raises(RuntimeError) as excinfo:
        llm._raw_generate('x', 10)
    error = excinfo.value
    assert error.__context__ is None
    assert error.__cause__ is None
    assert FAKE_KEY not in repr(error.__context__)
    assert FAKE_KEY not in ''.join(
        traceback.format_exception(type(error), error, error.__traceback__))


# --- structured calls: concurrency and cleanup --------------------------------

def test_concurrent_structured_calls_each_send_their_own_schema(oai):
    schema_a = dict(BOOL_SCHEMA)
    schema_b = {'type': 'object', 'properties': {'n': {'type': 'integer'}},
                'required': ['n'], 'additionalProperties': False}
    barrier = threading.Barrier(2, timeout=5)

    class Gate:
        per_minute = 0

        def wait(self):
            barrier.wait()  # both threads have set their schema before either builds a request

    oai.limiter = Gate()
    oai.script.results = [openai_reply('{"verdict":true}'), openai_reply('{"n":3}')]
    out = {}

    def run(key, name, schema):
        out[key] = oai.generate_structured('p', schema, name=name)

    threads = [threading.Thread(target=run, args=('a', 'sa', schema_a)),
               threading.Thread(target=run, args=('b', 'sb', schema_b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    sent = {r['text']['format']['name']: r['text']['format']['schema'] for r in oai.script.requests}
    assert sent == {'sa': schema_a, 'sb': schema_b}


def test_schema_cleared_when_generate_raises(oai):
    with patch.object(OpenAILLM, 'generate', side_effect=RuntimeError('boom')):
        with pytest.raises(RuntimeError):
            oai.generate_structured('c', VERDICT_SCHEMA)
    assert getattr(oai._local, 'schema', None) is None
    oai.script.results = [openai_reply('plain')]
    assert oai.generate('d') == 'plain'
    assert 'text' not in oai.script.requests[-1]


# --- alias canonicalisation ---------------------------------------------------

@pytest.mark.parametrize('primary, chain, expected', [
    ('claude', 'anthropic,claude', []),
    ('anthropic', 'claude', []),
    ('claude', 'Anthropic, openai', ['openai']),
    ('ollama', 'anthropic,claude,openai', ['anthropic', 'openai']),
    ('ollama', 'claude,anthropic', ['claude']),
])
def test_claude_aliases_dedupe_in_the_fallback_chain(monkeypatch, primary, chain, expected):
    monkeypatch.setenv('LLM_FALLBACK_PROVIDER', chain)
    assert LLMManager._fallback_names(primary) == expected


@pytest.mark.parametrize('raw', ['{"score":NaN}', '{"score":Infinity}', '{"score":-Infinity}', '{"score":1e999}'])
def test_non_finite_numbers_are_rejected_even_where_a_number_is_allowed(oai, raw):
    schema = {'type': 'object', 'properties': {'score': {'type': 'number'}},
              'required': ['score'], 'additionalProperties': False}
    oai.script.results = [openai_reply(raw)]
    assert oai.generate_structured('c', schema) is None


# ─────────────────────────────────────────────────────────────────────────────
# Round 2: the real SDKs, with a mocked transport and hostile environments
# ─────────────────────────────────────────────────────────────────────────────

real_anthropic = pytest.importorskip('anthropic')
real_openai = pytest.importorskip('openai')
httpx2 = pytest.importorskip('httpx2')

CLAUDE_BODY = {
    'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': 'claude-haiku-5-5',
    'content': [{'type': 'text', 'text': 'hi from claude'}], 'stop_reason': 'end_turn',
    'stop_sequence': None, 'usage': {'input_tokens': 1, 'output_tokens': 1},
}
OPENAI_BODY = {
    'id': 'resp_1', 'object': 'response', 'status': 'completed', 'model': 'gpt-6-luna',
    'output': [{'type': 'message', 'id': 'm1', 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': 'hi from openai', 'annotations': []}]}],
}


class Wire:
    """A mock transport that records every request the real SDK client sends."""

    def __init__(self, body, redirect_to=None):
        self.body, self.redirect_to, self.requests = body, redirect_to, []

    def __call__(self, request):
        self.requests.append(request)
        if self.redirect_to and len(self.requests) == 1:
            return httpx2.Response(307, headers={'location': self.redirect_to})
        return httpx2.Response(200, json=self.body)

    @property
    def hosts(self):
        return [r.url.host for r in self.requests]


def _real_provider(monkeypatch, which, wire):
    """The real SDK with its HTTP client swapped for the mock transport."""
    mod = real_anthropic if which == 'claude' else real_openai
    original = mod.DefaultHttpxClient
    monkeypatch.setattr(
        mod, 'DefaultHttpxClient',
        lambda **kw: original(transport=httpx2.MockTransport(wire), **kw))
    if which == 'claude':
        monkeypatch.setattr(claude_mod, 'anthropic', real_anthropic)
        monkeypatch.setattr(claude_mod, 'CLAUDE_AVAILABLE', True)
        monkeypatch.setenv('ANTHROPIC_API_KEY', FAKE_KEY)
        llm = ClaudeLLM()
    else:
        monkeypatch.setattr(openai_mod, 'openai', real_openai)
        monkeypatch.setattr(openai_mod, 'OPENAI_AVAILABLE', True)
        monkeypatch.setenv('OPENAI_API_KEY', FAKE_KEY)
        llm = OpenAILLM()
    llm.limiter = RateLimiter(0)
    return llm


REAL = [('claude', CLAUDE_BODY, 'api.anthropic.com'), ('openai', OPENAI_BODY, 'api.openai.com')]


@pytest.mark.parametrize('which, body, official', REAL)
def test_real_sdk_ignores_ambient_base_url_variables(monkeypatch, which, body, official):
    for var in ('OPENAI_BASE_URL', 'ANTHROPIC_BASE_URL'):
        monkeypatch.setenv(var, 'http://attacker.invalid/v1')
    wire = Wire(body)
    llm = _real_provider(monkeypatch, which, wire)
    assert llm.authenticate() is True
    assert llm.generate('hello') is not None
    assert wire.hosts == [official]
    # A reconnect rebuilds the client; it must be pinned too.
    assert llm._reconnect() is True
    assert llm.generate('hello') is not None
    assert wire.hosts == [official, official]


@pytest.mark.parametrize('which, body, official', REAL)
def test_real_sdk_does_not_follow_redirects_with_the_key(monkeypatch, which, body, official):
    wire = Wire(body, redirect_to='http://attacker.invalid/capture')
    llm = _real_provider(monkeypatch, which, wire)
    assert llm.authenticate() is True
    assert llm.generate('hello', max_retries=1) is None
    assert wire.hosts == [official], 'the redirect target must never be contacted'


@pytest.mark.parametrize('which, body, official', REAL)
def test_real_sdk_sends_the_key_only_to_the_pinned_host(monkeypatch, which, body, official):
    wire = Wire(body)
    llm = _real_provider(monkeypatch, which, wire)
    assert llm.authenticate() is True
    llm.generate('hello')
    request = wire.requests[0]
    assert request.url.host == official
    assert FAKE_KEY in (request.headers.get('x-api-key') or request.headers.get('authorization'))


def test_real_openai_configured_gateway_is_used(monkeypatch):
    wire = Wire(OPENAI_BODY)
    monkeypatch.setenv('LLM_OPENAI_BASE_URL', 'https://gateway.example/v1')
    monkeypatch.setenv('OPENAI_BASE_URL', 'http://attacker.invalid/v1')
    llm = _real_provider(monkeypatch, 'openai', wire)
    assert llm.authenticate() is True
    llm.generate('hello')
    assert wire.hosts == ['gateway.example']


@pytest.mark.parametrize('which', ['claude', 'oai'])
def test_clients_are_built_without_redirects(request, which):
    llm = request.getfixturevalue(which)
    assert llm.created[0]['http_client'].follow_redirects is False


# --- error context cannot leak a credential, whatever the call site -----------

def _assert_clean(error):
    assert error.__context__ is None
    assert error.__cause__ is None
    text = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
    assert FAKE_KEY not in text
    assert FAKE_KEY not in repr(error.__context__)


@pytest.mark.parametrize('fixture_name', ['claude', 'oai'])
def test_error_clean_inside_an_outer_except_block(request, fixture_name):
    llm = request.getfixturevalue(fixture_name)
    llm.script.results = [LeakyError()]
    try:
        raise ValueError(f'outer handler holds {FAKE_KEY}')
    except ValueError:
        with pytest.raises(RuntimeError) as excinfo:
            llm._raw_generate('x', 10)
    _assert_clean(excinfo.value)


@pytest.mark.parametrize('fixture_name', ['claude', 'oai'])
def test_error_clean_inside_an_async_handler(request, fixture_name):
    llm = request.getfixturevalue(fixture_name)
    llm.script.results = [LeakyError()]

    async def go():
        try:
            raise ValueError(f'outer handler holds {FAKE_KEY}')
        except ValueError:
            await asyncio.sleep(0)
            llm._raw_generate('x', 10)

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(go())
    _assert_clean(excinfo.value)


class BrokenStatusError(Exception):
    @property
    def status_code(self):
        raise ValueError(f'CLASSIFY-{FAKE_KEY}')


@pytest.mark.parametrize('fixture_name', ['claude', 'oai'])
def test_a_failing_classifier_degrades_to_a_generic_error(request, fixture_name):
    llm = request.getfixturevalue(fixture_name)
    llm.script.results = [BrokenStatusError(f'secret {FAKE_KEY}')]
    with pytest.raises(RuntimeError) as excinfo:
        llm._raw_generate('x', 10)
    assert type(excinfo.value) is not ValueError
    assert 'CLASSIFY' not in str(excinfo.value)
    _assert_clean(excinfo.value)
    assert 'CLASSIFY' not in ''.join(traceback.format_exception(
        type(excinfo.value), excinfo.value, excinfo.value.__traceback__))


# --- schema bounds and integers -----------------------------------------------

def _deep_list(depth):
    value = []
    for _ in range(depth):
        value = [value]
    return value


def test_deep_enum_values_are_refused_not_a_recursion_error(oai):
    schema = {'type': 'object', 'properties': {'v': {'enum': [_deep_list(600)]}},
              'required': ['v'], 'additionalProperties': False}
    oai.script.results = [openai_reply('{"v": 1}')]
    assert oai.generate_structured('c', schema) is None
    assert oai.script.requests == []


def test_oversized_enum_is_refused(oai):
    schema = {'type': 'object', 'properties': {'v': {'enum': list(range(100000))}},
              'required': ['v'], 'additionalProperties': False}
    assert oai.generate_structured('c', schema) is None
    assert oai.script.requests == []


def test_recursion_error_in_validation_returns_none(oai):
    oai.script.results = [openai_reply('{"verdict":true}')]
    with patch.object(openai_mod, 'validate_against_schema', side_effect=RecursionError):
        assert oai.generate_structured('c', BOOL_SCHEMA) is None


def test_overly_deep_schema_is_refused(oai):
    schema = {'type': 'string'}
    for _ in range(40):
        schema = {'type': 'object', 'properties': {'a': schema}}
    oai.script.results = [openai_reply('{}')]
    assert oai.generate_structured('c', schema) is None
    assert oai.script.requests == []


@pytest.mark.parametrize('keyword', [
    '$ref', '$defs', 'oneOf', 'anyOf', 'allOf', 'not', 'pattern', 'minimum', 'maximum',
    'minLength', 'maxLength', 'minItems', 'const', 'format', 'if', 'patternProperties'])
def test_every_unsupported_keyword_is_refused(oai, keyword):
    schema = dict(BOOL_SCHEMA, **{keyword: {'type': 'string'}})
    oai.script.results = [openai_reply('{"verdict":true}')]
    assert oai.generate_structured('c', schema) is None
    assert oai.script.requests == []


def test_enum_distinguishes_booleans_from_numbers(oai):
    for enum, raw in (([1], '{"v": true}'), ([True], '{"v": 1}'), ([0], '{"v": false}'),
                      ([False], '{"v": 0}')):
        schema = {'type': 'object', 'properties': {'v': {'enum': enum}},
                  'required': ['v'], 'additionalProperties': False}
        oai.script.results = [openai_reply(raw)]
        assert oai.generate_structured('c', schema) is None, (enum, raw)
    schema = {'type': 'object', 'properties': {'v': {'enum': [True]}},
              'required': ['v'], 'additionalProperties': False}
    oai.script.results = [openai_reply('{"v": true}')]
    assert oai.generate_structured('c', schema) == {'v': True}


@pytest.mark.parametrize('raw, ok', [
    ('1.0', True), ('1e0', True), ('3', True), ('-2.0', True),
    ('1.5', False), ('true', False), ('"1"', False), ('null', False)])
def test_integer_accepts_integral_json_numbers(raw, ok):
    from hypeman_social.llm._cloud import parse_strict_json, validate_against_schema
    assert validate_against_schema(parse_strict_json(raw), {'type': 'integer'}) is ok


# --- configuration parsing ----------------------------------------------------

def test_exact_config_reads_doppler_first_by_exact_name(monkeypatch):
    from hypeman_social.config.config import get_exact_config
    monkeypatch.setenv('DOPPLER_TOKEN', 'dp.test.fake')
    monkeypatch.setenv('LLM_OPENAI_MODEL', 'env-model')
    values = {'LLM_OPENAI_MODEL': 'doppler-model', 'OPENAI_BASE_URL': 'https://x.invalid/'}
    with patch('hypeman_social.config.secrets._doppler_project_values', return_value=values):
        assert get_exact_config('LLM_OPENAI_MODEL') == 'doppler-model'
        assert get_exact_config('LLM_OPENAI_BASE_URL') is None  # bare Doppler key is not read


def test_huge_integer_settings_fall_back_instead_of_overflowing(monkeypatch):
    monkeypatch.setenv('LLM_OPENAI_RATE_LIMIT', '9' * 400)
    monkeypatch.setenv('LLM_CLAUDE_RATE_LIMIT', '9' * 400)
    assert OpenAILLM().rate_limit == 60
    assert ClaudeLLM().rate_limit == 50


def test_zero_openai_rate_limit_means_unlimited(monkeypatch):
    monkeypatch.setenv('LLM_OPENAI_RATE_LIMIT', '0')
    assert OpenAILLM().rate_limit == 0


@pytest.mark.parametrize('var', [
    'MAX_TOKENS', 'LLM_MAX_TOKENS', 'LLM_MAX_RETRIES', 'LLM_RETRY_DELAY_BASE', 'LLM_TEMPERATURE',
    'LLM_TOP_P', 'LLM_RECONNECT_INTERVAL', 'LLM_DEDUP_CACHE_SIZE', 'LLM_THINKING_TOKEN_MULTIPLIER'])
def test_shared_numeric_settings_cannot_crash_the_cloud_constructors(monkeypatch, caplog, var):
    monkeypatch.setenv(var, 'oops')
    caplog.set_level(logging.WARNING)
    assert OpenAILLM().max_tokens == 150
    assert ClaudeLLM().max_retries == 3
    assert var in caplog.text


# --- aliases ------------------------------------------------------------------

def test_primary_name_is_normalised_before_the_comparison(monkeypatch):
    from hypeman_social.llm.manager import canonical_provider_name
    assert canonical_provider_name('  Anthropic ') == 'claude'
    assert canonical_provider_name('OpenAI') == 'openai'
    monkeypatch.setenv('LLM_FALLBACK_PROVIDER', 'anthropic,openai')
    assert LLMManager._fallback_names(' CLAUDE ') == ['openai']
