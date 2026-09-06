# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Starred-repository rendering on every platform, ported from Star-Daemon's
connectors: Discord gets a rich embed with repo fields, Bluesky an external
card built from API metadata, Mastodon a text card, Matrix HTML paragraphs.
"""

from types import SimpleNamespace

import pytest

import hypeman_social.social.bluesky as bluesky_module
from hypeman_social.social.base import EVENT_STAR
from hypeman_social.social.bluesky import BlueskyPlatform
from hypeman_social.social.discord import DiscordPlatform
from hypeman_social.social.mastodon import MastodonPlatform
from hypeman_social.social.matrix import MatrixPlatform
from tests.test_bluesky_post import FakeBskyClient, FakeModels, FakeTextBuilder
from tests.test_social_platforms import FakeResponse

REPO_DATA = {
    'full_name': 'ChiefGyk3D/hypeman',
    'name': 'hypeman',
    'description': 'Shared core for announcement daemons',
    'language': 'Python',
    'stargazers_count': 1234,
    'forks_count': 56,
    'owner': {'avatar_url': 'https://avatars.example/chief.png'},
}

REPO_URL = 'https://github.com/ChiefGyk3D/hypeman'
STAR_DATA = {'event_kind': EVENT_STAR, 'repo_data': REPO_DATA, 'url': REPO_URL}


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


class TestDiscordStarEmbed:
    def _post(self, discord, monkeypatch, message=f'Cool project! {REPO_URL}',
              stream_data=STAR_DATA):
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured['url'] = url
            captured['json'] = json
            return FakeResponse(200, {'id': '9'})

        monkeypatch.setattr('hypeman_social.social.discord.requests.post', fake_post)
        result = discord.post(message, platform_name='github', stream_data=stream_data)
        return result, captured

    def test_github_star_gets_purple_embed(self, discord, monkeypatch):
        result, captured = self._post(discord, monkeypatch)
        assert result == '9'
        embed = captured['json']['embeds'][0]
        assert embed['title'] == '⭐ Starred on GitHub'
        assert embed['color'] == 0x6E5494
        assert embed['url'] == REPO_URL
        assert embed['footer'] == {'text': 'Click to view repository'}

    def test_message_lives_in_description_not_content(self, discord, monkeypatch):
        _, captured = self._post(discord, monkeypatch)
        embed = captured['json']['embeds'][0]
        assert embed['description'].startswith('Cool project!')
        # No role configured, so the content line is omitted entirely.
        assert 'content' not in captured['json']

    def test_repo_fields_rendered(self, discord, monkeypatch):
        _, captured = self._post(discord, monkeypatch)
        fields = {f['name']: f['value'] for f in captured['json']['embeds'][0]['fields']}
        assert fields['📦 Repository'] == 'ChiefGyk3D/hypeman'
        assert fields['📝 Description'] == 'Shared core for announcement daemons'
        assert fields['💻 Language'] == 'Python'
        assert fields['⭐ Stars'] == '1,234'
        assert fields['🔀 Forks'] == '56'

    def test_owner_avatar_is_thumbnail(self, discord, monkeypatch):
        _, captured = self._post(discord, monkeypatch)
        embed = captured['json']['embeds'][0]
        assert embed['thumbnail'] == {'url': 'https://avatars.example/chief.png'}

    def test_gitlab_star_gets_orange_embed(self, discord, monkeypatch):
        url = 'https://gitlab.com/group/project'
        data = {'event_kind': EVENT_STAR, 'repo_data': {'name': 'project'}}
        _, captured = self._post(discord, monkeypatch, message=f'neat {url}', stream_data=data)
        embed = captured['json']['embeds'][0]
        assert embed['title'] == '⭐ Starred on GitLab'
        assert embed['color'] == 0xFC6D26

    def test_unknown_host_gets_gold_embed(self, discord, monkeypatch):
        url = 'https://codeberg.org/chief/thing'
        data = {'event_kind': EVENT_STAR, 'repo_data': {'name': 'thing'}}
        _, captured = self._post(discord, monkeypatch, message=f'star {url}', stream_data=data)
        embed = captured['json']['embeds'][0]
        assert embed['title'] == '⭐ New Starred Repository'
        assert embed['color'] == 0xFFD700

    def test_role_mention_is_sole_content(self, monkeypatch):
        monkeypatch.setenv('DISCORD_ENABLE_POSTING', 'true')
        monkeypatch.setenv('DISCORD_WEBHOOK_URL', WEBHOOK)
        monkeypatch.setenv('DISCORD_ROLE', '777')
        platform = DiscordPlatform()
        platform.authenticate()

        captured = {}
        monkeypatch.setattr(
            'hypeman_social.social.discord.requests.post',
            lambda url, json=None, timeout=None: (captured.update(json=json), FakeResponse(200, {'id': '1'}))[1])
        platform.post(f'wow {REPO_URL}', stream_data=STAR_DATA)
        assert captured['json']['content'] == '<@&777>'

    def test_live_events_unaffected(self, discord, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            'hypeman_social.social.discord.requests.post',
            lambda url, json=None, timeout=None: (captured.update(json=json), FakeResponse(200, {'id': '1'}))[1])
        discord.post('live https://twitch.tv/chief', platform_name='twitch',
                     stream_data={'title': 'Speedrun', 'viewer_count': 3})
        embed = captured['json']['embeds'][0]
        assert 'Twitch' in embed['title']
        assert captured['json']['content'].startswith('live')


# ─────────────────────────────────────────────────────────────────────────
# Bluesky
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def bluesky(monkeypatch):
    monkeypatch.setattr(bluesky_module, 'client_utils',
                        SimpleNamespace(TextBuilder=FakeTextBuilder))
    monkeypatch.setattr(bluesky_module, 'models', FakeModels)
    instance = BlueskyPlatform()
    instance.enabled = True
    instance.authenticated = True
    instance.client = FakeBskyClient()
    return instance


class TestBlueskyRepoCard:
    def test_repo_metadata_becomes_external_card(self, bluesky, monkeypatch):
        # The avatar fetch must not hit the network.
        monkeypatch.setattr('requests.get',
                            lambda *a, **k: FakeResponse(200))
        uri = bluesky.post(f'Great tool! {REPO_URL}', stream_data=STAR_DATA)
        assert uri == 'at://post/1'
        external = bluesky.client.sent[0]['embed'].external
        assert external.uri == REPO_URL
        assert external.title == 'ChiefGyk3D/hypeman'
        assert external.description.startswith('⭐ 1,234 stars • Python')
        assert 'Shared core for announcement daemons' in external.description

    def test_avatar_uploaded_as_thumb(self, bluesky, monkeypatch):
        monkeypatch.setattr('requests.get',
                            lambda *a, **k: FakeResponse(200))
        bluesky.post(f'star {REPO_URL}', stream_data=STAR_DATA)
        assert bluesky.client.sent[0]['embed'].external.thumb is not None

    def test_thumb_failure_still_posts_card(self, bluesky, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError('network down')
        monkeypatch.setattr('requests.get', boom)
        uri = bluesky.post(f'star {REPO_URL}', stream_data=STAR_DATA)
        assert uri == 'at://post/1'
        external = bluesky.client.sent[0]['embed'].external
        assert external.thumb is None
        assert external.title == 'ChiefGyk3D/hypeman'


# ─────────────────────────────────────────────────────────────────────────
# Mastodon
# ─────────────────────────────────────────────────────────────────────────

class FakeMastodonClient:
    def __init__(self):
        self.statuses = []
        self.media = []

    def status_post(self, message, in_reply_to_id=None, media_ids=None):
        self.statuses.append({'message': message, 'reply': in_reply_to_id,
                              'media_ids': media_ids})
        return {'id': len(self.statuses)}

    def media_post(self, path, description=None):
        self.media.append({'path': path, 'description': description})
        return {'id': f'media-{len(self.media)}'}


@pytest.fixture
def mastodon():
    platform = MastodonPlatform()
    platform.enabled = True
    platform.authenticated = True
    platform.client = FakeMastodonClient()
    return platform


class TestMastodonRepoCard:
    def test_repo_card_appended_to_status(self, mastodon, monkeypatch):
        monkeypatch.setattr('requests.get', lambda *a, **k: FakeResponse(404))
        result = mastodon.post(f'Check this out {REPO_URL}', stream_data=STAR_DATA)
        assert result == '1'
        status = mastodon.client.statuses[0]['message']
        assert status.startswith('Check this out')
        assert '⭐ 1,234 stars • Python' in status
        assert 'Shared core for announcement daemons' in status

    def test_avatar_uploaded_with_repo_alt_text(self, mastodon, monkeypatch):
        response = FakeResponse(200)
        response.headers = {'content-type': 'image/png'}
        response.content = b'png-bytes'
        monkeypatch.setattr('requests.get', lambda *a, **k: response)

        mastodon.post(f'star {REPO_URL}', stream_data=STAR_DATA)
        assert mastodon.client.media[0]['description'] == '⭐ ChiefGyk3D/hypeman'
        assert mastodon.client.statuses[0]['media_ids'] == ['media-1']

    def test_card_without_description(self, mastodon):
        data = {'event_kind': EVENT_STAR,
                'repo_data': {'name': 'thing', 'stargazers_count': 5}}
        mastodon.post('star!', stream_data=data)
        assert mastodon.client.statuses[0]['message'] == 'star!\n\n⭐ 5 stars'

    def test_stream_posts_unchanged(self, mastodon):
        mastodon.post('live now!', stream_data={'title': 'Speedrun'})
        assert mastodon.client.statuses[0]['message'] == 'live now!'


# ─────────────────────────────────────────────────────────────────────────
# Matrix
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def matrix():
    platform = MatrixPlatform()
    platform.enabled = True
    platform.authenticated = True
    platform.homeserver = 'https://matrix.example'
    platform.access_token = 'token'
    platform.room_id = '!room:matrix.example'
    return platform


class TestMatrixStarMessage:
    def _post(self, matrix, monkeypatch, message, stream_data):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured['json'] = json
            return FakeResponse(200, {'event_id': '$evt'})

        monkeypatch.setattr('hypeman_social.social.matrix.requests.post', fake_post)
        result = matrix.post(message, stream_data=stream_data)
        return result, captured

    def test_github_star_heading_and_repo_card(self, matrix, monkeypatch):
        result, captured = self._post(matrix, monkeypatch,
                                      f'Nice one {REPO_URL}', STAR_DATA)
        assert result == '$evt'
        html = captured['json']['formatted_body']
        assert '<strong>⭐ Starred on GitHub</strong>' in html
        assert '<strong>📦 ChiefGyk3D/hypeman</strong>' in html
        assert '<em>Shared core for announcement daemons</em>' in html
        assert '💻 Python • ⭐ 1,234 stars' in html
        # The plain-text body stays untouched for clients without HTML.
        assert captured['json']['body'] == f'Nice one {REPO_URL}'

    def test_non_github_star_gets_generic_heading(self, matrix, monkeypatch):
        url = 'https://codeberg.org/chief/thing'
        data = {'event_kind': EVENT_STAR, 'repo_data': {'name': 'thing'}}
        _, captured = self._post(matrix, monkeypatch, f'star {url}', data)
        assert '<strong>⭐ New Star</strong>' in captured['json']['formatted_body']

    def test_live_styling_unaffected(self, matrix, monkeypatch):
        _, captured = self._post(matrix, monkeypatch,
                                 'live https://twitch.tv/chief', {'title': 't'})
        assert '🟣 Live on Twitch!' in captured['json']['formatted_body']
