# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""One placeholder rule: empty and template values count as unset, never sent anywhere."""

import logging

import pytest

from hypeman_social.config import get_secret, is_placeholder
from hypeman_social.social import platform_secret
from hypeman_social.social.discord import DiscordPlatform
from hypeman_social.social.matrix import MatrixPlatform

PLACEHOLDERS = [
    '', '   ', 'YOUR_API_KEY', 'your_api_key', 'Your_Api_Key', 'your-token',
    'CHANGEME', 'changeme123', 'change_me', 'change-me', '<token>', '<your token here>',
    'example', 'Placeholder', 'xxx', 'XXXXXXXX', 'dp.st.your_token',
    'https://discord.com/api/webhooks/123456/YOUR_WEBHOOK_TOKEN',
    'https://discord.com/api/webhooks/123456/your_webhook_token',
    'https://discord.com/api/webhooks/<id>/<token>',
]

REAL = [
    'abc123SECRET', 'sk-ant-api03-realvalue', 'dp.st.prd.abcdef0123456789',
    'https://discord.com/api/webhooks/123456/aBcD-eF_gH',
    'matrix.example.org', 'hunter2', 'yours_truly_token_9', 'exemplar',
]

SECRET_VALUE = 'YOUR_SUPER_SECRET_TOKEN_VALUE'


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in ('DOPPLER_TOKEN', 'SECRETS_MANAGER', 'ANTHROPIC_API_KEY',
                 'OPENAI_API_KEY', 'GEMINI_API_KEY'):
        monkeypatch.delenv(name, raising=False)


class TestRule:
    @pytest.mark.parametrize('value', PLACEHOLDERS)
    def test_placeholder_detected(self, value):
        assert is_placeholder(value) is True

    @pytest.mark.parametrize('value', REAL)
    def test_real_value_accepted(self, value):
        assert is_placeholder(value) is False

    def test_none_is_placeholder(self):
        assert is_placeholder(None) is True

    def test_non_string_real_values_pass(self):
        assert is_placeholder(12345) is False


class TestLookups:
    @pytest.mark.parametrize('value', PLACEHOLDERS)
    def test_env_placeholder_reads_as_unset(self, monkeypatch, value):
        monkeypatch.setenv('ACME_TOKEN', value)
        assert get_secret('Acme', 'token') is None
        assert get_secret('Acme', 'token', default='d') == 'd'

    @pytest.mark.parametrize('value', REAL)
    def test_env_real_value_unchanged(self, monkeypatch, value):
        monkeypatch.setenv('ACME_TOKEN', value)
        assert get_secret('Acme', 'token') == value
        assert platform_secret('Acme', 'token') == value

    def test_lowercase_placeholder_logs_variable_not_value(self, monkeypatch, caplog):
        monkeypatch.setenv('ACME_TOKEN', SECRET_VALUE.lower())
        with caplog.at_level(logging.DEBUG):
            assert get_secret('Acme', 'token') is None
        assert 'ACME_TOKEN' in caplog.text
        assert SECRET_VALUE.lower() not in caplog.text.lower()

    def test_aws_bundle_placeholder_skipped(self, monkeypatch):
        from hypeman_social.config import secrets

        monkeypatch.setenv('SECRETS_MANAGER', 'aws')
        monkeypatch.setattr(secrets, 'load_secrets_from_aws',
                            lambda name: {'token': 'your_token_here'})
        assert get_secret('Acme', 'token') is None

    def test_explicit_override_placeholder_reads_as_unset(self, monkeypatch):
        from hypeman_social.social.base import SocialPlatform

        class P(SocialPlatform):
            def authenticate(self):
                return False

            def post(self, *a, **k):
                return None

        monkeypatch.delenv('P_TOKEN', raising=False)
        assert P('P', token='your_token').credential('token') is None
        assert P('P', token='real-value').credential('token') == 'real-value'


class TestFeaturesStayDisabled:
    @pytest.fixture(autouse=True)
    def no_network(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError('network call attempted with a placeholder')

        monkeypatch.setattr('requests.post', boom)
        monkeypatch.setattr('requests.get', boom)
        monkeypatch.setattr('requests.request', boom)

    def test_discord_placeholder_webhook(self, monkeypatch, caplog):
        monkeypatch.setenv('DISCORD_ENABLE_POSTING', 'true')
        monkeypatch.setenv(
            'DISCORD_WEBHOOK_URL',
            'https://discord.com/api/webhooks/1/YOUR_WEBHOOK_TOKEN')
        with caplog.at_level(logging.INFO):
            assert DiscordPlatform().authenticate() is False
        assert 'DISCORD_WEBHOOK_URL' in caplog.text
        assert 'YOUR_WEBHOOK_TOKEN' not in caplog.text

    def test_discord_per_platform_placeholder_ignored(self, monkeypatch):
        monkeypatch.setenv('DISCORD_ENABLE_POSTING', 'true')
        monkeypatch.delenv('DISCORD_WEBHOOK_URL', raising=False)
        monkeypatch.setenv('DISCORD_WEBHOOK_YOUTUBE', 'your_webhook')
        assert DiscordPlatform().authenticate() is False

    def test_matrix_placeholder_password_no_login(self, monkeypatch, caplog):
        monkeypatch.setenv('MATRIX_ENABLE_POSTING', 'true')
        monkeypatch.setenv('MATRIX_HOMESERVER', 'https://matrix.example.org')
        monkeypatch.setenv('MATRIX_ROOM_ID', '!room:example.org')
        monkeypatch.setenv('MATRIX_USERNAME', 'bot')
        monkeypatch.setenv('MATRIX_PASSWORD', 'changeme')
        monkeypatch.delenv('MATRIX_ACCESS_TOKEN', raising=False)
        with caplog.at_level(logging.INFO):
            assert MatrixPlatform().authenticate() is False
        assert 'changeme' not in caplog.text

    @pytest.mark.parametrize('var', ['ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'GEMINI_API_KEY'])
    def test_llm_env_fallback_placeholder_rejected(self, monkeypatch, caplog, var):
        from hypeman_social.llm.claude import ClaudeLLM
        from hypeman_social.llm.gemini import GeminiLLM
        from hypeman_social.llm.openai_llm import OpenAILLM

        cls = {'ANTHROPIC_API_KEY': ClaudeLLM, 'OPENAI_API_KEY': OpenAILLM,
               'GEMINI_API_KEY': GeminiLLM}[var]
        # The anthropic and openai SDKs are absent on Python 3.9; the key check
        # runs before any client is built, so mark the SDK present to reach it.
        for module, flag in (('claude', 'CLAUDE_AVAILABLE'),
                             ('openai_llm', 'OPENAI_AVAILABLE'),
                             ('gemini', 'GEMINI_AVAILABLE')):
            monkeypatch.setattr(f'hypeman_social.llm.{module}.{flag}', True)
        monkeypatch.setenv(var, SECRET_VALUE.lower())
        with caplog.at_level(logging.INFO):
            assert cls().authenticate() is False
        assert var in caplog.text
        assert SECRET_VALUE.lower() not in caplog.text.lower()
