# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
The social layer: base helpers, and each platform's authenticate/post flow
with the network mocked out. Nothing here talks to a real service.
"""

import pytest

from hypeman_social.social import REGISTRY
from hypeman_social.social.base import (
    EVENT_LIVE,
    EVENT_UPLOAD,
    SocialPlatform,
    event_kind,
    is_url_for_domain,
)
from hypeman_social.social.bluesky import BlueskyPlatform, _count_graphemes
from hypeman_social.social.discord import DiscordPlatform
from hypeman_social.social.mastodon import MastodonPlatform
from hypeman_social.social.matrix import MatrixPlatform


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=''):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self.headers = {}
        self.content = b''

    def json(self):
        return self._payload


# ─────────────────────────────────────────────────────────────────────────
# base.py helpers
# ─────────────────────────────────────────────────────────────────────────

class TestIsUrlForDomain:
    def test_exact_domain(self):
        assert is_url_for_domain('https://youtube.com/watch?v=x', 'youtube.com')

    def test_subdomain(self):
        assert is_url_for_domain('https://www.youtube.com/watch?v=x', 'youtube.com')

    def test_lookalike_domain_rejected(self):
        assert not is_url_for_domain('https://evil-youtube.com.attacker.net/x', 'youtube.com')

    def test_substring_prefix_rejected(self):
        assert not is_url_for_domain('https://notyoutube.com/x', 'youtube.com')

    def test_empty_and_garbage(self):
        assert not is_url_for_domain('', 'youtube.com')
        assert not is_url_for_domain('not a url', 'youtube.com')

    def test_case_insensitive(self):
        assert is_url_for_domain('https://YouTube.COM/x', 'youtube.com')


class TestEventKind:
    def test_explicit_kind_wins(self):
        assert event_kind({'event_kind': EVENT_UPLOAD}, default=EVENT_LIVE) == EVENT_UPLOAD

    def test_default_when_missing(self):
        assert event_kind({}, default=EVENT_LIVE) == EVENT_LIVE
        assert event_kind(None, default=EVENT_UPLOAD) == EVENT_UPLOAD


class RecordingPlatform(SocialPlatform):
    """Minimal concrete platform for exercising the base class."""

    def __init__(self, **credentials):
        super().__init__('Recording', **credentials)
        self.posted = []
        self.raise_on_post = False

    def authenticate(self):
        self.enabled = True
        self.authenticated = True
        return True

    def post(self, message, reply_to_id=None, platform_name=None, stream_data=None):
        if self.raise_on_post:
            raise RuntimeError('network exploded')
        self.posted.append(message)
        return 'post-id-1'


class TestSocialPlatformBase:
    def test_credential_override_beats_config(self, monkeypatch):
        monkeypatch.setenv('RECORDING_TOKEN', 'from-env')
        platform = RecordingPlatform(token='injected')
        assert platform.credential('token') == 'injected'

    def test_credential_falls_back_to_env(self, monkeypatch):
        monkeypatch.setenv('RECORDING_TOKEN', 'from-env')
        platform = RecordingPlatform()
        assert platform.credential('token') == 'from-env'

    def test_none_override_is_ignored(self, monkeypatch):
        monkeypatch.setenv('RECORDING_TOKEN', 'from-env')
        platform = RecordingPlatform(token=None)
        assert platform.credential('token') == 'from-env'

    def test_safe_post_skips_when_not_ready(self):
        platform = RecordingPlatform()
        assert platform.safe_post('hello') is None
        assert platform.posted == []

    def test_safe_post_posts_when_ready(self):
        platform = RecordingPlatform()
        platform.authenticate()
        assert platform.safe_post('hello') == 'post-id-1'
        assert platform.posted == ['hello']

    def test_safe_post_never_raises(self):
        platform = RecordingPlatform()
        platform.authenticate()
        platform.raise_on_post = True
        assert platform.safe_post('hello') is None
        assert 'RuntimeError' in platform.status()['last_error']

    def test_status_shape(self):
        status = RecordingPlatform().status()
        assert set(status) == {'name', 'enabled', 'authenticated', 'last_error'}


class TestRegistry:
    def test_all_networks_present(self):
        assert set(REGISTRY) == {'bluesky', 'mastodon', 'discord', 'matrix', 'threads'}

    def test_names_match_keys(self):
        for key, cls in REGISTRY.items():
            assert cls().name.lower() == key


# ─────────────────────────────────────────────────────────────────────────
# Discord
# ─────────────────────────────────────────────────────────────────────────

WEBHOOK = 'https://discord.example/api/webhooks/1/token'


@pytest.fixture
def discord(monkeypatch):
    monkeypatch.setenv('DISCORD_ENABLE_POSTING', 'true')
    monkeypatch.setenv('DISCORD_WEBHOOK_URL', WEBHOOK)
    platform = DiscordPlatform()
    assert platform.authenticate() is True
    return platform


class TestDiscord:
    def test_disabled_without_config(self, monkeypatch):
        monkeypatch.delenv('DISCORD_ENABLE_POSTING', raising=False)
        assert DiscordPlatform().authenticate() is False

    def test_enabled_without_webhook_fails(self, monkeypatch):
        monkeypatch.setenv('DISCORD_ENABLE_POSTING', 'true')
        monkeypatch.delenv('DISCORD_WEBHOOK_URL', raising=False)
        assert DiscordPlatform().authenticate() is False

    def test_post_hits_webhook_with_wait(self, discord, monkeypatch):
        calls = {}

        def fake_post(url, json=None, timeout=None):
            calls['url'] = url
            calls['json'] = json
            return FakeResponse(200, {'id': '42'})

        monkeypatch.setattr('hypeman_social.social.discord.requests.post', fake_post)
        result = discord.post('go watch https://twitch.tv/chief', platform_name='twitch',
                              stream_data={'title': 'Speedrun', 'viewer_count': 12})

        assert result == '42'
        assert calls['url'] == WEBHOOK + '?wait=true'
        embed = calls['json']['embeds'][0]
        assert embed['url'] == 'https://twitch.tv/chief'
        assert 'Twitch' in embed['title']
        assert discord.active_messages['twitch']['message_id'] == '42'

    def test_upload_event_gets_video_embed(self, discord, monkeypatch):
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured['json'] = json
            return FakeResponse(200, {'id': '7'})

        monkeypatch.setattr('hypeman_social.social.discord.requests.post', fake_post)
        discord.post('new vid https://youtube.com/watch?v=abc', platform_name='youtube',
                     stream_data={'title': 'My Video', 'event_kind': EVENT_UPLOAD})

        embed = captured['json']['embeds'][0]
        assert 'New YouTube Video' in embed['title']
        # Uploads deliberately carry no viewer-count fields.
        assert 'fields' not in embed

    def test_role_mention_appended(self, monkeypatch):
        monkeypatch.setenv('DISCORD_ENABLE_POSTING', 'true')
        monkeypatch.setenv('DISCORD_WEBHOOK_URL', WEBHOOK)
        monkeypatch.setenv('DISCORD_ROLE', '999')
        platform = DiscordPlatform()
        platform.authenticate()

        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured['json'] = json
            return FakeResponse(200, {'id': '7'})

        monkeypatch.setattr('hypeman_social.social.discord.requests.post', fake_post)
        platform.post('hello')
        assert captured['json']['content'].endswith('<@&999>')

    def test_per_platform_webhook_selected(self, monkeypatch):
        monkeypatch.setenv('DISCORD_ENABLE_POSTING', 'true')
        monkeypatch.setenv('DISCORD_WEBHOOK_URL', WEBHOOK)
        monkeypatch.setenv('DISCORD_WEBHOOK_TWITCH', WEBHOOK + '-twitch')
        platform = DiscordPlatform()
        platform.authenticate()

        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured['url'] = url
            return FakeResponse(200, {'id': '7'})

        monkeypatch.setattr('hypeman_social.social.discord.requests.post', fake_post)
        platform.post('live!', platform_name='twitch')
        assert captured['url'].startswith(WEBHOOK + '-twitch')

    def test_failed_post_returns_none(self, discord, monkeypatch):
        monkeypatch.setattr('hypeman_social.social.discord.requests.post',
                            lambda *a, **k: FakeResponse(404))
        assert discord.post('hello') is None

    def test_update_stream_patches_message(self, discord, monkeypatch):
        monkeypatch.setattr('hypeman_social.social.discord.requests.post',
                            lambda *a, **k: FakeResponse(200, {'id': '42'}))
        discord.post('live https://twitch.tv/chief', platform_name='twitch',
                     stream_data={'title': 't', 'viewer_count': 1})

        captured = {}

        def fake_patch(url, json=None, timeout=None):
            captured['url'] = url
            captured['json'] = json
            return FakeResponse(200)

        monkeypatch.setattr('hypeman_social.social.discord.requests.patch', fake_patch)
        ok = discord.update_stream('twitch', {'title': 't', 'viewer_count': 55},
                                   'https://twitch.tv/chief')
        assert ok is True
        assert captured['url'] == f'{WEBHOOK}/messages/42'
        fields = captured['json']['embeds'][0]['fields']
        assert any(f['value'] == '55' for f in fields)

    def test_end_stream_clears_tracking(self, discord, monkeypatch):
        monkeypatch.setattr('hypeman_social.social.discord.requests.post',
                            lambda *a, **k: FakeResponse(200, {'id': '42'}))
        discord.post('live https://twitch.tv/chief', platform_name='twitch',
                     stream_data={'title': 't'})
        monkeypatch.setattr('hypeman_social.social.discord.requests.patch',
                            lambda *a, **k: FakeResponse(200))
        assert discord.end_stream('twitch', {'title': 't'}, 'https://twitch.tv/chief') is True
        assert 'twitch' not in discord.active_messages

    def test_update_without_tracked_message_is_noop(self, discord):
        assert discord.update_stream('kick', {}, 'https://kick.com/x') is False


# ─────────────────────────────────────────────────────────────────────────
# Matrix
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def matrix_env(monkeypatch):
    monkeypatch.setenv('MATRIX_ENABLE_POSTING', 'true')
    monkeypatch.setenv('MATRIX_HOMESERVER', 'https://matrix.example')
    monkeypatch.setenv('MATRIX_ROOM_ID', '!room:matrix.example')
    monkeypatch.delenv('MATRIX_USERNAME', raising=False)
    monkeypatch.delenv('MATRIX_PASSWORD', raising=False)


class TestMatrix:
    def test_token_auth(self, matrix_env, monkeypatch):
        monkeypatch.setenv('MATRIX_ACCESS_TOKEN', 'tok')
        platform = MatrixPlatform()
        assert platform.authenticate() is True
        assert platform.access_token == 'tok'

    def test_missing_everything_fails(self, matrix_env, monkeypatch):
        monkeypatch.delenv('MATRIX_ACCESS_TOKEN', raising=False)
        assert MatrixPlatform().authenticate() is False

    def test_password_login_wins_over_token(self, matrix_env, monkeypatch):
        monkeypatch.setenv('MATRIX_ACCESS_TOKEN', 'stale')
        monkeypatch.setenv('MATRIX_USERNAME', '@bot:matrix.example')
        monkeypatch.setenv('MATRIX_PASSWORD', 'hunter2')

        captured = {}

        def fake_post(url, json=None, timeout=None, headers=None):
            captured['url'] = url
            captured['json'] = json
            return FakeResponse(200, {'access_token': 'fresh'})

        monkeypatch.setattr('hypeman_social.social.matrix.requests.post', fake_post)
        platform = MatrixPlatform()
        assert platform.authenticate() is True
        assert platform.access_token == 'fresh'
        # MXID must be reduced to the localpart for the login call.
        assert captured['json']['identifier']['user'] == 'bot'

    def test_post_builds_html_and_returns_event_id(self, matrix_env, monkeypatch):
        monkeypatch.setenv('MATRIX_ACCESS_TOKEN', 'tok')
        platform = MatrixPlatform()
        platform.authenticate()

        captured = {}

        def fake_post(url, json=None, timeout=None, headers=None):
            captured['url'] = url
            captured['json'] = json
            captured['headers'] = headers
            return FakeResponse(200, {'event_id': '$evt'})

        monkeypatch.setattr('hypeman_social.social.matrix.requests.post', fake_post)
        result = platform.post('live https://twitch.tv/chief', reply_to_id='$parent')

        assert result == '$evt'
        assert captured['headers']['Authorization'] == 'Bearer tok'
        assert '<a href=' in captured['json']['formatted_body']
        assert captured['json']['m.relates_to']['m.in_reply_to']['event_id'] == '$parent'

    def test_failed_post_returns_none(self, matrix_env, monkeypatch):
        monkeypatch.setenv('MATRIX_ACCESS_TOKEN', 'tok')
        platform = MatrixPlatform()
        platform.authenticate()
        monkeypatch.setattr('hypeman_social.social.matrix.requests.post',
                            lambda *a, **k: FakeResponse(403, text='nope'))
        assert platform.post('hello') is None


# ─────────────────────────────────────────────────────────────────────────
# Mastodon
# ─────────────────────────────────────────────────────────────────────────

class FakeMastodonClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.statuses = []

    def status_post(self, message, in_reply_to_id=None, media_ids=None):
        self.statuses.append((message, in_reply_to_id, media_ids))
        return {'id': 12345}

    def media_post(self, path, description=None):
        return {'id': 'media-1'}


@pytest.fixture
def mastodon_env(monkeypatch):
    monkeypatch.setenv('MASTODON_ENABLE_POSTING', 'true')
    monkeypatch.setenv('MASTODON_CLIENT_ID', 'cid')
    monkeypatch.setenv('MASTODON_CLIENT_SECRET', 'csec')
    monkeypatch.setenv('MASTODON_ACCESS_TOKEN', 'tok')
    monkeypatch.setenv('MASTODON_API_BASE_URL', 'https://mastodon.example')
    monkeypatch.setattr('hypeman_social.social.mastodon.Mastodon', FakeMastodonClient)


class TestMastodon:
    def test_authenticate_and_post_thread(self, mastodon_env):
        platform = MastodonPlatform()
        assert platform.authenticate() is True

        result = platform.post('hello fediverse', reply_to_id='999')
        assert result == '12345'
        assert platform.client.statuses[0] == ('hello fediverse', '999', None)

    def test_missing_credentials_fail(self, mastodon_env, monkeypatch):
        monkeypatch.delenv('MASTODON_ACCESS_TOKEN', raising=False)
        assert MastodonPlatform().authenticate() is False

    def test_thumbnail_uploaded_and_attached(self, mastodon_env, monkeypatch):
        platform = MastodonPlatform()
        platform.authenticate()

        image = FakeResponse(200)
        image.headers = {'content-type': 'image/jpeg'}
        image.content = b'fake-jpeg-bytes'
        monkeypatch.setattr('requests.get', lambda *a, **k: image)

        result = platform.post('new vid', stream_data={'thumbnail_url': 'https://i.example/t.jpg',
                                                       'viewer_count': 3, 'game_name': 'Zelda'})
        assert result == '12345'
        message, reply_to, media_ids = platform.client.statuses[0]
        assert media_ids == ['media-1']

    def test_thumbnail_failure_still_posts(self, mastodon_env, monkeypatch):
        platform = MastodonPlatform()
        platform.authenticate()

        def explode(*a, **k):
            raise RuntimeError('cdn down')

        monkeypatch.setattr('requests.get', explode)
        result = platform.post('new vid', stream_data={'thumbnail_url': 'https://i.example/t.jpg'})
        assert result == '12345'
        assert platform.client.statuses[0][2] is None

    def test_post_before_auth_returns_none(self, mastodon_env):
        assert MastodonPlatform().post('hello') is None


# ─────────────────────────────────────────────────────────────────────────
# Bluesky (pure logic — no network)
# ─────────────────────────────────────────────────────────────────────────

class TestBlueskyTruncation:
    def test_short_message_untouched(self):
        message = 'short and sweet https://twitch.tv/chief'
        assert BlueskyPlatform._truncate_to_limit(message) == message

    def test_long_message_keeps_url(self):
        url = 'https://youtube.com/watch?v=abcdefgh'
        message = ('w' * 400) + ' ' + url
        result = BlueskyPlatform._truncate_to_limit(message)
        assert _count_graphemes(result) <= 300
        assert url in result

    def test_hard_truncation_when_url_dominates(self):
        message = 'hi https://example.com/' + ('a' * 400)
        result = BlueskyPlatform._truncate_to_limit(message)
        assert _count_graphemes(result) <= 300

    def test_emoji_counted_as_graphemes(self):
        # 150 family emoji are 150 graphemes but far more code points; the
        # limit must be applied to graphemes, not len().
        message = '👨‍👩‍👧‍👦' * 150
        result = BlueskyPlatform._truncate_to_limit(message)
        assert _count_graphemes(result) <= 300

    def test_no_url_truncates_at_word_boundary(self):
        message = 'word ' * 100
        result = BlueskyPlatform._truncate_to_limit(message)
        assert _count_graphemes(result) <= 300
        assert result.endswith('...')

    def test_disabled_without_config(self, monkeypatch):
        monkeypatch.delenv('BLUESKY_ENABLE_POSTING', raising=False)
        assert BlueskyPlatform().authenticate() is False

    def test_post_before_auth_returns_none(self):
        assert BlueskyPlatform().post('hello') is None
