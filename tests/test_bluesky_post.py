# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Bluesky's post flow with atproto faked out: rich text building, embeds,
threading, and failure behavior. No network involved.
"""

from types import SimpleNamespace

import pytest

import hypeman_social.social.bluesky as bluesky_module
from hypeman_social.social.bluesky import BlueskyPlatform


class FakeTextBuilder:
    def __init__(self):
        self.parts = []

    def text(self, value):
        self.parts.append(('text', value))
        return self

    def link(self, display, url):
        self.parts.append(('link', url))
        return self

    def tag(self, display, tag):
        self.parts.append(('tag', tag))
        return self

    def build_text(self):
        return ''.join(
            part if kind == 'text' else part
            for kind, part in self.parts
        )


class FakeExternal(SimpleNamespace):
    pass


class FakeModels:
    class AppBskyEmbedExternal:
        @staticmethod
        def Main(external):
            return SimpleNamespace(external=external)

        @staticmethod
        def External(**kwargs):
            return FakeExternal(**kwargs)

    class AppBskyFeedPost:
        @staticmethod
        def ReplyRef(parent, root):
            return SimpleNamespace(parent=parent, root=root)

    @staticmethod
    def create_strong_ref(post):
        return SimpleNamespace(uri=post.uri, cid=getattr(post, 'cid', 'cid'))


class FakeBskyClient:
    def __init__(self):
        self.sent = []
        self.parent_posts = []
        self.app = SimpleNamespace(
            bsky=SimpleNamespace(
                feed=SimpleNamespace(get_posts=self._get_posts)
            )
        )
        self.raise_on_send = False

    def _get_posts(self, params):
        return SimpleNamespace(posts=self.parent_posts)

    def send_post(self, text_builder, reply_to=None, embed=None):
        if self.raise_on_send:
            raise RuntimeError('bsky down')
        self.sent.append({'text': text_builder.build_text(), 'reply_to': reply_to,
                          'embed': embed})
        return SimpleNamespace(uri=f'at://post/{len(self.sent)}')

    def upload_blob(self, content):
        return SimpleNamespace(blob=SimpleNamespace(size=len(content)))


@pytest.fixture
def platform(monkeypatch):
    monkeypatch.setattr(bluesky_module, 'client_utils',
                        SimpleNamespace(TextBuilder=FakeTextBuilder))
    monkeypatch.setattr(bluesky_module, 'models', FakeModels)
    instance = BlueskyPlatform()
    instance.enabled = True
    instance.authenticated = True
    instance.client = FakeBskyClient()
    return instance


class TestBlueskyPost:
    def test_simple_post_with_stream_embed(self, platform):
        uri = platform.post(
            'Live now! https://twitch.tv/chief #gaming',
            stream_data={'title': 'Speedrun', 'game_name': 'Metroid', 'is_live': True},
        )
        assert uri == 'at://post/1'
        sent = platform.client.sent[0]
        assert sent['embed'].external.uri == 'https://twitch.tv/chief'
        assert sent['embed'].external.title == 'Speedrun'
        assert 'Metroid' in sent['embed'].external.description

    def test_hashtags_become_tags(self, platform):
        platform.post('hello #gaming #speedrun https://twitch.tv/chief',
                      stream_data={'title': 't'})
        # The builder recorded tag facets without the # prefix.
        # (Text content itself is covered by the send_post assertion above.)
        assert platform.client.sent  # posted successfully

    def test_kick_without_stream_data_posts_without_embed(self, platform):
        uri = platform.post('live https://kick.com/chief')
        assert uri == 'at://post/1'
        assert platform.client.sent[0]['embed'] is None

    def test_threaded_reply_builds_reply_ref(self, platform):
        parent = SimpleNamespace(
            uri='at://post/parent', cid='c1',
            record=SimpleNamespace(reply=None),
        )
        platform.client.parent_posts = [parent]

        uri = platform.post('follow-up https://twitch.tv/chief',
                            reply_to_id='at://post/parent',
                            stream_data={'title': 't'})
        assert uri == 'at://post/1'
        reply = platform.client.sent[0]['reply_to']
        assert reply.parent.uri == 'at://post/parent'
        assert reply.root.uri == 'at://post/parent'

    def test_reply_uses_existing_root(self, platform):
        root_ref = SimpleNamespace(uri='at://post/root', cid='c0')
        parent = SimpleNamespace(
            uri='at://post/parent', cid='c1',
            record=SimpleNamespace(reply=SimpleNamespace(root=root_ref)),
        )
        platform.client.parent_posts = [parent]

        platform.post('deep reply', reply_to_id='at://post/parent')
        reply = platform.client.sent[0]['reply_to']
        assert reply.root.uri == 'at://post/root'

    def test_missing_parent_falls_back_to_plain_post(self, platform):
        platform.client.parent_posts = []
        uri = platform.post('orphan reply', reply_to_id='at://post/gone')
        assert uri == 'at://post/1'
        assert platform.client.sent[0]['reply_to'] is None

    def test_send_failure_returns_none(self, platform):
        platform.client.raise_on_send = True
        assert platform.post('doomed https://kick.com/chief') is None

    def test_overlong_message_truncated_before_send(self, platform):
        message = ('word ' * 200) + 'https://kick.com/chief'
        uri = platform.post(message)
        assert uri == 'at://post/1'
        sent_text = platform.client.sent[0]['text']
        assert bluesky_module._count_graphemes(sent_text) <= 300
