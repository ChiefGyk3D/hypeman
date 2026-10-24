# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Discord webhook hardening: the webhook token stays out of log records, and
every payload carries allowed_mentions so text such as a stream title cannot
ping @everyone. The network is mocked; nothing here talks to Discord.
"""

import logging

import pytest
import requests

from hypeman_social.social.discord import DiscordPlatform

TOKEN = 'SECRETtoken123'
WEBHOOK = f'https://discord.example/api/webhooks/1234567890/{TOKEN}'
STREAM_URL = 'https://twitch.tv/chief'
MOD = 'hypeman_social.social.discord'


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def make_platform(monkeypatch, webhook=WEBHOOK, role=None, twitch_role=None):
    monkeypatch.setenv('DISCORD_ENABLE_POSTING', 'true')
    monkeypatch.setenv('DISCORD_WEBHOOK_URL', webhook)
    for name in ('DISCORD_ROLE', 'DISCORD_ROLE_TWITCH'):
        monkeypatch.delenv(name, raising=False)
    if role:
        monkeypatch.setenv('DISCORD_ROLE', role)
    if twitch_role:
        monkeypatch.setenv('DISCORD_ROLE_TWITCH', twitch_role)
    platform = DiscordPlatform()
    assert platform.authenticate() is True
    return platform


def posted(platform, monkeypatch, message='live', **kwargs):
    """Post once with the network mocked; return the payload that was sent."""
    sent = {}

    def fake_post(url, json=None, timeout=None):
        sent['json'] = json
        return FakeResponse(200, {'id': '42'})

    monkeypatch.setattr(f'{MOD}.requests.post', fake_post)
    assert platform.post(message, platform_name='twitch', **kwargs) == '42'
    return sent['json']


def patched(monkeypatch):
    sent = {}

    def fake_patch(url, json=None, timeout=None):
        sent['json'] = json
        return FakeResponse(200)

    monkeypatch.setattr(f'{MOD}.requests.patch', fake_patch)
    return sent


def assert_no_token(caplog, *secrets):
    assert caplog.records, 'nothing was logged: the test collected nothing'
    for record in caplog.records:
        rendered = record.getMessage() + str(record.args) + str(record.exc_text)
        for secret in secrets:
            assert secret not in rendered
    for secret in secrets:
        assert secret not in caplog.text


def raising(exc_factory):
    def _raise(*args, **kwargs):
        raise exc_factory(args[0])
    return _raise


# -- the token stays out of the logs ---------------------------------------

class TestTokenNotLogged:
    def test_post_connection_error(self, monkeypatch, caplog):
        platform = make_platform(monkeypatch)
        monkeypatch.setattr(
            f'{MOD}.requests.post',
            raising(lambda url: requests.ConnectionError(
                f"HTTPSConnectionPool(host='discord.example', port=443): Max retries "
                f"exceeded with url: /api/webhooks/1234567890/{TOKEN}?wait=true")))
        with caplog.at_level(logging.DEBUG):
            assert platform.post('hello', platform_name='twitch') is None
        assert 'Discord post failed' in caplog.text
        assert_no_token(caplog, TOKEN)

    def test_post_http_error_with_full_url(self, monkeypatch, caplog):
        platform = make_platform(monkeypatch)
        monkeypatch.setattr(
            f'{MOD}.requests.post',
            raising(lambda url: requests.HTTPError(
                f'500 Server Error: Internal Server Error for url: {url}')))
        with caplog.at_level(logging.DEBUG):
            assert platform.post('hello', platform_name='twitch') is None
        assert 'Discord post failed' in caplog.text
        assert_no_token(caplog, TOKEN)

    def test_update_stream_error(self, monkeypatch, caplog):
        platform = make_platform(monkeypatch)
        posted(platform, monkeypatch)
        monkeypatch.setattr(
            f'{MOD}.requests.patch',
            raising(lambda url: requests.ConnectionError(f'cannot reach {url}')))
        with caplog.at_level(logging.DEBUG):
            assert platform.update_stream('twitch', {'title': 't'}, STREAM_URL) is False
        assert 'Discord update failed' in caplog.text
        assert_no_token(caplog, TOKEN)

    def test_end_stream_error(self, monkeypatch, caplog):
        platform = make_platform(monkeypatch)
        posted(platform, monkeypatch)
        monkeypatch.setattr(
            f'{MOD}.requests.patch',
            raising(lambda url: requests.HTTPError(f'404 Client Error for url: {url}')))
        with caplog.at_level(logging.DEBUG):
            assert platform.end_stream('twitch', {'title': 't'}, STREAM_URL) is False
        assert 'Discord stream ended update failed' in caplog.text
        assert_no_token(caplog, TOKEN)

    def test_bare_webhook_string_without_api_path(self, monkeypatch, caplog):
        """A proxy-style URL has no /api/webhooks/ path; the held string is redacted."""
        proxy = f'https://hooks.example/relay/{TOKEN}'
        platform = make_platform(monkeypatch, webhook=proxy)
        monkeypatch.setattr(
            f'{MOD}.requests.post',
            raising(lambda url: requests.ConnectionError(f'failed: {url}')))
        with caplog.at_level(logging.DEBUG):
            assert platform.post('hello', platform_name='twitch') is None
        assert 'Discord post failed' in caplog.text
        assert_no_token(caplog, TOKEN)

    def test_path_only_message_from_urllib3_is_redacted(self, monkeypatch, caplog):
        """urllib3 reports 'Max retries exceeded with url: /path' (no host), so a
        proxy URL whose token sits in a non-standard path must be caught by path."""
        proxy = f'https://hooks.example/relay/v2/{TOKEN}'
        platform = make_platform(monkeypatch, webhook=proxy)
        text = ("HTTPSConnectionPool(host='hooks.example', port=443): Max retries "
                f"exceeded with url: /relay/v2/{TOKEN}?wait=true "
                "(Caused by NewConnectionError('boom'))")
        monkeypatch.setattr(f'{MOD}.requests.post',
                            raising(lambda url: requests.ConnectionError(text)))
        with caplog.at_level(logging.DEBUG):
            assert platform.post('hello', platform_name='twitch') is None
        assert 'Discord post failed' in caplog.text
        assert 'hooks.example' in caplog.text or 'Max retries' in caplog.text
        assert_no_token(caplog, TOKEN)

    def test_percent_encoded_path_is_redacted(self, monkeypatch, caplog):
        token = 'tok/en with space'
        proxy = 'https://hooks.example/relay/tok%2Fen%20with%20space'
        platform = make_platform(monkeypatch, webhook=proxy)
        text = 'Max retries exceeded with url: /relay/tok%2Fen%20with%20space?wait=true'
        monkeypatch.setattr(f'{MOD}.requests.post',
                            raising(lambda url: requests.ConnectionError(text)))
        with caplog.at_level(logging.DEBUG):
            assert platform.post('hello', platform_name='twitch') is None
        assert 'tok%2Fen' not in caplog.text and token not in caplog.text

    def test_per_platform_webhook_string_redacted(self, monkeypatch, caplog):
        other = f'https://hooks.example/other/{TOKEN}-twitch'
        monkeypatch.setenv('DISCORD_WEBHOOK_TWITCH', other)
        platform = make_platform(monkeypatch)
        monkeypatch.setattr(
            f'{MOD}.requests.post',
            raising(lambda url: requests.ConnectionError(f'failed: {url}')))
        with caplog.at_level(logging.DEBUG):
            assert platform.post('hello', platform_name='twitch') is None
        assert_no_token(caplog, TOKEN)

    def test_error_class_and_unrelated_text_survive(self, monkeypatch, caplog):
        """Negative case: redaction removes the secret only, not the diagnosis."""
        platform = make_platform(monkeypatch)
        monkeypatch.setattr(
            f'{MOD}.requests.post',
            raising(lambda url: requests.ConnectionError('connection refused by peer')))
        with caplog.at_level(logging.DEBUG):
            platform.post('hello', platform_name='twitch')
        assert 'ConnectionError' in caplog.text
        assert 'connection refused by peer' in caplog.text


# -- allowed_mentions on every payload -------------------------------------

NO_ROLE = {'parse': []}


class TestAllowedMentions:
    def test_post_without_role_parses_nothing(self, monkeypatch):
        platform = make_platform(monkeypatch)
        data = posted(platform, monkeypatch, message='hi @everyone and @here')
        assert data['allowed_mentions'] == NO_ROLE

    def test_post_with_default_role_allows_only_that_role(self, monkeypatch):
        platform = make_platform(monkeypatch, role='999')
        data = posted(platform, monkeypatch, message='hi @everyone')
        assert data['content'].endswith('<@&999>')
        assert data['allowed_mentions'] == {'parse': [], 'roles': ['999']}

    def test_platform_role_wins_over_default(self, monkeypatch):
        platform = make_platform(monkeypatch, role='999', twitch_role='555')
        data = posted(platform, monkeypatch)
        assert data['content'].endswith('<@&555>')
        assert data['allowed_mentions'] == {'parse': [], 'roles': ['555']}

    def test_star_embed_payload(self, monkeypatch):
        platform = make_platform(monkeypatch, role='999')
        data = posted(platform, monkeypatch, message='starred https://github.com/a/b',
                      stream_data={'event_kind': 'star', 'repo_data': {'name': 'b'}})
        assert data['allowed_mentions'] == {'parse': [], 'roles': ['999']}

    def test_update_keeps_the_same_role(self, monkeypatch):
        platform = make_platform(monkeypatch, role='999', twitch_role='555')
        posted(platform, monkeypatch)
        sent = patched(monkeypatch)
        assert platform.update_stream('twitch', {'title': '@everyone'}, STREAM_URL) is True
        assert sent['json']['allowed_mentions'] == {'parse': [], 'roles': ['555']}
        assert sent['json']['content'].endswith('<@&555>')

    def test_update_without_role(self, monkeypatch):
        platform = make_platform(monkeypatch)
        posted(platform, monkeypatch)
        sent = patched(monkeypatch)
        assert platform.update_stream('twitch', {'title': 't'}, STREAM_URL) is True
        assert sent['json']['allowed_mentions'] == NO_ROLE

    def test_end_stream_keeps_the_same_role(self, monkeypatch):
        platform = make_platform(monkeypatch, role='999', twitch_role='555')
        posted(platform, monkeypatch)
        sent = patched(monkeypatch)
        assert platform.end_stream('twitch', {'title': '@here'}, STREAM_URL) is True
        assert sent['json']['allowed_mentions'] == {'parse': [], 'roles': ['555']}
        assert sent['json']['content'] == '<@&555>'

    def test_end_stream_without_role(self, monkeypatch):
        platform = make_platform(monkeypatch)
        posted(platform, monkeypatch)
        sent = patched(monkeypatch)
        assert platform.end_stream('twitch', {'title': 't'}, STREAM_URL) is True
        assert sent['json']['allowed_mentions'] == NO_ROLE
        assert 'content' not in sent['json']

    @pytest.mark.parametrize('parse_key', ['everyone', 'users', 'roles'])
    def test_nothing_is_auto_parsed(self, monkeypatch, parse_key):
        platform = make_platform(monkeypatch, role='999')
        data = posted(platform, monkeypatch)
        assert parse_key not in data['allowed_mentions']['parse']
