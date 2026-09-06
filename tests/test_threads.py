# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
ThreadsPlatform: the two-step container-create + publish flow, faked out.
"""

import pytest

from hypeman_social.social.threads import THREADS_CHAR_LIMIT, ThreadsPlatform
from tests.test_social_platforms import FakeResponse


@pytest.fixture
def threads(monkeypatch):
    monkeypatch.setenv('THREADS_ENABLE_POSTING', 'true')
    monkeypatch.setenv('THREADS_ACCESS_TOKEN', 'tok')
    monkeypatch.setenv('THREADS_USER_ID', '12345')
    platform = ThreadsPlatform()
    assert platform.authenticate() is True
    return platform


class FakeThreadsAPI:
    """Records the container/publish calls and hands back IDs."""

    def __init__(self, container_status=200, publish_status=200):
        self.calls = []
        self.container_status = container_status
        self.publish_status = publish_status

    def post(self, url, data=None, timeout=None):
        self.calls.append({'url': url, 'data': data})
        if url.endswith('/threads'):
            return FakeResponse(self.container_status, {'id': 'container-1'})
        if url.endswith('/threads_publish'):
            return FakeResponse(self.publish_status, {'id': 'media-1'})
        raise AssertionError(f'unexpected URL {url}')


class TestAuthenticate:
    def test_disabled_without_config(self, monkeypatch):
        monkeypatch.delenv('THREADS_ENABLE_POSTING', raising=False)
        assert ThreadsPlatform().authenticate() is False

    def test_missing_token_fails(self, monkeypatch):
        monkeypatch.setenv('THREADS_ENABLE_POSTING', 'true')
        monkeypatch.delenv('THREADS_ACCESS_TOKEN', raising=False)
        monkeypatch.setenv('THREADS_USER_ID', '12345')
        assert ThreadsPlatform().authenticate() is False

    def test_credential_overrides(self, monkeypatch):
        monkeypatch.setenv('THREADS_ENABLE_POSTING', 'true')
        platform = ThreadsPlatform(access_token='injected', user_id='42')
        assert platform.authenticate() is True
        assert platform.access_token == 'injected'


class TestPost:
    def test_create_then_publish(self, threads, monkeypatch):
        api = FakeThreadsAPI()
        monkeypatch.setattr('hypeman_social.social.threads.requests.post', api.post)

        result = threads.post('New video! https://youtube.com/watch?v=abc')

        assert result == 'media-1'
        create, publish = api.calls
        assert create['url'].endswith('/12345/threads')
        assert create['data']['media_type'] == 'TEXT'
        assert create['data']['text'].startswith('New video!')
        assert create['data']['link_attachment'] == 'https://youtube.com/watch?v=abc'
        assert publish['url'].endswith('/12345/threads_publish')
        assert publish['data']['creation_id'] == 'container-1'

    def test_reply_threading(self, threads, monkeypatch):
        api = FakeThreadsAPI()
        monkeypatch.setattr('hypeman_social.social.threads.requests.post', api.post)
        threads.post('follow-up', reply_to_id='media-0')
        assert api.calls[0]['data']['reply_to_id'] == 'media-0'

    def test_no_link_attachment_without_url(self, threads, monkeypatch):
        api = FakeThreadsAPI()
        monkeypatch.setattr('hypeman_social.social.threads.requests.post', api.post)
        threads.post('just words')
        assert 'link_attachment' not in api.calls[0]['data']

    def test_failed_container_returns_none(self, threads, monkeypatch):
        api = FakeThreadsAPI(container_status=400)
        monkeypatch.setattr('hypeman_social.social.threads.requests.post', api.post)
        assert threads.post('hello') is None
        assert len(api.calls) == 1  # publish never attempted

    def test_failed_publish_returns_none(self, threads, monkeypatch):
        api = FakeThreadsAPI(publish_status=500)
        monkeypatch.setattr('hypeman_social.social.threads.requests.post', api.post)
        assert threads.post('hello') is None

    def test_network_error_returns_none(self, threads, monkeypatch):
        def boom(*a, **k):
            raise ConnectionError('down')
        monkeypatch.setattr('hypeman_social.social.threads.requests.post', boom)
        assert threads.post('hello') is None

    def test_long_message_trimmed_keeps_url(self, threads, monkeypatch):
        api = FakeThreadsAPI()
        monkeypatch.setattr('hypeman_social.social.threads.requests.post', api.post)
        url = 'https://youtube.com/watch?v=abc'
        threads.post('word ' * 200 + f'\n\n{url}')
        sent = api.calls[0]['data']['text']
        assert len(sent) <= THREADS_CHAR_LIMIT
        assert sent.endswith(url)


class TestConnection:
    def test_probe_hits_me_endpoint(self, threads, monkeypatch):
        captured = {}

        def fake_get(url, params=None, timeout=None):
            captured['url'] = url
            captured['params'] = params
            return FakeResponse(200, {'id': '12345'})

        monkeypatch.setattr('hypeman_social.social.threads.requests.get', fake_get)
        assert threads.test_connection() is True
        assert captured['url'].endswith('/me')

    def test_probe_failure_is_false_not_raise(self, threads, monkeypatch):
        def boom(*a, **k):
            raise ConnectionError('down')
        monkeypatch.setattr('hypeman_social.social.threads.requests.get', boom)
        assert threads.test_connection() is False
