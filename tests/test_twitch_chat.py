# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
The Twitch chat adapter with the websocket and the HTTP layer faked. Synthetic
ids and obviously fake secrets only; nothing touches the network.
"""

import base64
import collections
import hashlib
import json
import logging
import queue
import socket
import struct
import threading
import time

import pytest

from hypeman_social.social import twitch as tw
from hypeman_social.social.events import (
    ROLE_BROADCASTER,
    ROLE_MODERATOR,
    ROLE_SUBSCRIBER,
    ROLE_VIP,
)
from hypeman_social.social.twitch import TwitchChat, clip_message, event_to_chat_event

ACCESS = 'ACCESSSECRET-aaaa1111'
REFRESH = 'REFRESHSECRET-bbbb2222'
CLIENT_SECRET = 'CLIENTSECRET-cccc3333'
NEW_ACCESS = 'NEWACCESSSECRET-dddd4444'
NEW_REFRESH = 'NEWREFRESHSECRET-eeee5555'
SECRETS = [ACCESS, REFRESH, CLIENT_SECRET, NEW_ACCESS, NEW_REFRESH]

CREDS = {
    'client_id': 'fakeclientid',
    'client_secret': CLIENT_SECRET,
    'access_token': ACCESS,
    'refresh_token': REFRESH,
    'bot_user_id': '222',
    'channel': '111',
}


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._body


def valid_body(**over):
    body = {'client_id': 'fakeclientid', 'user_id': '222', 'login': 'fakebot',
            'scopes': ['user:read:chat', 'user:write:chat'], 'expires_in': 10000}
    body.update(over)
    return Resp(200, body)


class FakeHttp:
    """Scripted responses per (method, url-suffix); a list is consumed in order."""

    def __init__(self):
        self.routes = {}
        self.calls = []
        self.lock = threading.Lock()

    def on(self, method, suffix, *responses):
        self.routes[(method, suffix)] = list(responses)

    def _do(self, method, url, **kw):
        with self.lock:
            self.calls.append((method, url, kw))
            item = self._next(method, url)
        if isinstance(item, Exception):
            raise item
        return item

    def _next(self, method, url):
        for (m, suffix), queue_ in self.routes.items():
            if m == method and url.endswith(suffix):
                item = queue_.pop(0) if len(queue_) > 1 else queue_[0]
                return item
        raise AssertionError(f'unexpected {method} {url}')

    def get(self, url, **kw):
        return self._do('get', url, **kw)

    def post(self, url, **kw):
        return self._do('post', url, **kw)

    def posts(self, suffix):
        return [c for c in self.calls if c[0] == 'post' and c[1].endswith(suffix)]


class FakeWs:
    def __init__(self, frames=()):
        self.q = queue.Queue()
        for f in frames:
            self.q.put(f)
        self.closed = False
        self.timeout = None

    def settimeout(self, t):
        self.timeout = t

    def recv(self):
        if self.closed:
            raise ConnectionError('closed')
        try:
            item = self.q.get(timeout=0.05)
        except queue.Empty:
            raise TimeoutError('quiet') from None
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, str) else json.dumps(item)

    def close(self):
        self.closed = True


def welcome(sid='sess1', keepalive=10, reconnect_url=None):
    return {'metadata': {'message_type': 'session_welcome'},
            'payload': {'session': {'id': sid, 'keepalive_timeout_seconds': keepalive,
                                    'reconnect_url': reconnect_url}}}


def chat_event(**over):
    event = {
        'broadcaster_user_id': '111', 'broadcaster_user_login': 'fakechannel',
        'chatter_user_id': '333', 'chatter_user_login': 'viewer', 'chatter_user_name': 'Viewer',
        'message_id': 'm-1', 'message': {'text': 'hello'}, 'badges': [], 'reply': None,
        'message_type': 'text',
    }
    event.update(over)
    return event


def iso(offset=0.0):
    """An RFC 3339 UTC timestamp with nanoseconds, as EventSub sends them."""
    t = time.time() + offset
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(t)) + '.%09dZ' % int((t % 1) * 1e9)


def notification(event, mid='n-1', sub_type='channel.chat.message', ts=None):
    return {'metadata': {'message_type': 'notification', 'message_id': mid,
                         'message_timestamp': iso() if ts is None else ts,
                         'subscription_type': sub_type},
            'payload': {'event': event}}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def http():
    h = FakeHttp()
    h.on('get', '/oauth2/validate', valid_body())
    h.on('post', '/eventsub/subscriptions', Resp(202))
    h.on('post', '/chat/messages', Resp(200, {'data': [{'message_id': 'x', 'is_sent': True,
                                                         'drop_reason': None}]}))
    return h


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    values = dict(CREDS)
    monkeypatch.setattr(tw, 'platform_secret', lambda platform, key, default=None:
                        values.get(key, default))
    return values


def make(http, clock, **kw):
    return TwitchChat(http=http, connect=kw.pop('connect', lambda url, t: FakeWs()),
                      clock=clock, backoff_base=0.001, long_rest=0.01, auth_poll=0.01, **kw)


# ----- conversion ---------------------------------------------------------

def test_event_conversion_roles_reply_and_ids():
    ev = event_to_chat_event(chat_event(
        badges=[{'set_id': 'moderator', 'id': '1', 'info': ''},
                {'set_id': 'subscriber', 'id': '3', 'info': '3'},
                {'set_id': 'vip', 'id': '1', 'info': ''},
                {'set_id': 'bits', 'id': '100', 'info': ''}],
        reply={'parent_message_id': 'parent-9'}), bot_user_id='222')
    assert ev.platform == 'twitch'
    assert ev.author_id == '333' and ev.author_name == 'Viewer'
    assert ev.channel == 'fakechannel'
    assert ev.roles == {ROLE_MODERATOR, ROLE_SUBSCRIBER, ROLE_VIP}
    assert ev.reply_to_id == 'parent-9'
    assert ev.is_self is False and ev.bridged is False


def test_broadcaster_role_and_is_self():
    ev = event_to_chat_event(chat_event(chatter_user_id='111'), bot_user_id='222')
    assert ROLE_BROADCASTER in ev.roles
    me = event_to_chat_event(chat_event(chatter_user_id='222'), bot_user_id='222')
    assert me.is_self is True


def test_login_lookalike_is_not_self():
    # A viewer whose login equals the bot's id string is still a different user id.
    ev = event_to_chat_event(chat_event(chatter_user_id='333', chatter_user_login='222'), '222')
    assert ev.is_self is False


def test_malformed_event_is_none():
    assert event_to_chat_event({'message_id': 'a'}, '222') is None
    assert event_to_chat_event({}, '222') is None


# ----- send ---------------------------------------------------------------

def test_send_ok_and_body(http, clock):
    chat = make(http, clock)
    assert chat.send('111', 'hi there', reply_to_id='p1') is True
    method, url, kw = http.posts('/chat/messages')[0]
    assert kw['json'] == {'broadcaster_id': '111', 'sender_id': '222', 'message': 'hi there',
                          'reply_parent_message_id': 'p1'}
    assert kw['allow_redirects'] is False


def test_send_honours_drop_reason(http, clock, caplog):
    http.on('post', '/chat/messages', Resp(200, {'data': [
        {'message_id': '', 'is_sent': False,
         'drop_reason': {'code': 'automod_held', 'message': 'held'}}]}))
    chat = make(http, clock)
    with caplog.at_level(logging.DEBUG):
        assert chat.send('111', 'hi') is False
    assert 'automod_held' in caplog.text


def test_send_refuses_other_channel(http, clock):
    chat = make(http, clock)
    assert chat.send('999', 'hi') is False
    assert not http.posts('/chat/messages')


def test_send_never_raises(http, clock):
    http.on('post', '/chat/messages', RuntimeError('boom'))
    chat = make(http, clock)
    assert chat.send('111', 'hi') is False
    assert chat.send(None, None) is False  # garbage in, False out


@pytest.mark.parametrize('status', [400, 401, 403, 422, 500])
def test_send_http_errors_return_false(http, clock, status):
    http.on('post', '/chat/messages', Resp(status))
    http.on('post', '/oauth2/token', Resp(400, {'message': 'Invalid refresh token'}))
    chat = make(http, clock)
    assert chat.send('111', 'hi') is False


def test_send_truncates_at_limit():
    assert len(clip_message('a' * 900)) <= 500
    assert clip_message('a b ' * 200).endswith('…')
    assert clip_message('line one\nline two') == 'line one line two'
    assert clip_message('short') == 'short'


def test_send_truncation_reaches_api(http, clock):
    chat = make(http, clock)
    chat.send('111', 'x' * 2000)
    assert len(http.posts('/chat/messages')[0][2]['json']['message']) <= 500


def test_rate_limiter_returns_false_not_blocks(http, clock):
    chat = make(http, clock)
    stamps = []
    for _ in range(40):
        clock.now += 1.6  # beyond the per-second spacing, faster than 15 per 30 s
        if chat.send('111', 'hi'):
            stamps.append(clock.now)
    assert len(stamps) < 40
    for t in stamps:  # never more than the cap inside any 30 s window
        assert len([x for x in stamps if t - 30 < x <= t]) <= tw.SEND_WINDOW_MAX
    clock.now += tw.SEND_WINDOW_SECONDS + 1
    assert chat.send('111', 'hi') is True


def test_rate_limiter_min_interval(http, clock):
    chat = make(http, clock)
    assert chat.send('111', 'a') is True
    assert chat.send('111', 'b') is False


def test_429_backs_off(http, clock):
    http.on('post', '/chat/messages', Resp(429, headers={}))
    chat = make(http, clock)
    assert chat.send('111', 'a') is False
    clock.now += 5
    http.on('post', '/chat/messages', Resp(200, {'data': [{'is_sent': True}]}))
    assert chat.send('111', 'b') is False  # still inside the 30 s quiet period
    clock.now += 40
    assert chat.send('111', 'c') is True


def test_send_401_refreshes_and_retries(http, clock):
    http.on('get', '/oauth2/validate', valid_body(), Resp(401), valid_body())
    http.on('post', '/chat/messages', Resp(401), Resp(200, {'data': [{'is_sent': True}]}))
    http.on('post', '/oauth2/token', Resp(200, {'access_token': NEW_ACCESS,
                                                 'refresh_token': NEW_REFRESH}))
    chat = make(http, clock)
    assert chat.send('111', 'hi') is True
    last = http.posts('/chat/messages')[-1][2]['headers']['Authorization']
    assert last == f'Bearer {NEW_ACCESS}'


# ----- auth ---------------------------------------------------------------

def test_validation_failure_marks_unavailable_one_error(http, clock, caplog):
    http.on('get', '/oauth2/validate', Resp(401))
    http.on('post', '/oauth2/token', Resp(400, {'message': 'Invalid refresh token'}))
    chat = make(http, clock)
    with caplog.at_level(logging.DEBUG):
        assert chat.is_available() is False
        for _ in range(5):
            assert chat.is_available() is False
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    # No retry storm: one validate and one refresh inside the cooldown.
    assert len([c for c in http.calls if c[1].endswith('/oauth2/token')]) == 1
    assert len([c for c in http.calls if c[1].endswith('/oauth2/validate')]) == 1


def test_auth_heals_after_cooldown(http, clock):
    http.on('get', '/oauth2/validate', Resp(401), valid_body())
    http.on('post', '/oauth2/token', Resp(400, {}))
    chat = make(http, clock)
    assert chat.is_available() is False
    clock.now += tw.AUTH_COOLDOWN + 1
    assert chat.is_available() is True


def test_refresh_path_success(http, clock, caplog):
    http.on('get', '/oauth2/validate', Resp(401), valid_body())
    http.on('post', '/oauth2/token', Resp(200, {'access_token': NEW_ACCESS,
                                                 'refresh_token': NEW_REFRESH,
                                                 'expires_in': 14000}))
    chat = make(http, clock)
    with caplog.at_level(logging.DEBUG):
        assert chat.is_available() is True
    body = http.posts('/oauth2/token')[0][2]['data']
    assert body['grant_type'] == 'refresh_token' and body['refresh_token'] == REFRESH
    assert 'memory only' in caplog.text


def test_refresh_when_nearly_expired(http, clock):
    http.on('get', '/oauth2/validate', valid_body(expires_in=60), valid_body())
    http.on('post', '/oauth2/token', Resp(200, {'access_token': NEW_ACCESS,
                                                 'refresh_token': NEW_REFRESH}))
    chat = make(http, clock)
    assert chat.is_available() is True
    assert len(http.posts('/oauth2/token')) == 1


def test_hourly_revalidation(http, clock):
    chat = make(http, clock)
    assert chat.is_available() is True
    chat.is_available()
    assert len([c for c in http.calls if c[1].endswith('/oauth2/validate')]) == 1
    clock.now += tw.VALIDATE_INTERVAL + 1
    chat.is_available()
    assert len([c for c in http.calls if c[1].endswith('/oauth2/validate')]) == 2


@pytest.mark.parametrize('body', [
    {'client_id': 'someoneelse'},
    {'user_id': '999'},
    {'scopes': ['user:read:chat']},
])
def test_wrong_app_user_or_scopes_unavailable(http, clock, body):
    http.on('get', '/oauth2/validate', valid_body(**body))
    assert make(http, clock).is_available() is False


def test_missing_credentials_unavailable(http, clock, creds):
    creds['access_token'] = ''
    assert make(http, clock).is_available() is False


def test_transient_validation_failure_keeps_working_token(http, clock):
    chat = make(http, clock)
    assert chat.is_available() is True
    http.on('get', '/oauth2/validate', Resp(500))
    clock.now += tw.VALIDATE_INTERVAL + 1
    assert chat.is_available() is True


def test_no_secret_in_logs_or_exceptions(http, clock, caplog):
    # Every failure mode at DEBUG; the recognisable secrets must never appear.
    http.on('get', '/oauth2/validate', Resp(401), Resp(401))
    http.on('post', '/oauth2/token', Resp(200, {'access_token': NEW_ACCESS,
                                                 'refresh_token': NEW_REFRESH}),
            RuntimeError(f'leaky {ACCESS} {CLIENT_SECRET}'))
    http.on('post', '/chat/messages', RuntimeError(f'Bearer {ACCESS}'))
    chat = make(http, clock)
    with caplog.at_level(logging.DEBUG):
        chat.is_available()
        chat.send('111', 'hi')
        clock.now += tw.AUTH_COOLDOWN + 1
        chat.is_available()
        chat.send('111', 'hi')
    blob = caplog.text + repr(chat) + str(chat)
    for secret in SECRETS:
        assert secret not in blob


# ----- receive ------------------------------------------------------------

def run_chat(chat, events_wanted=1, wait=3.0):
    got = []
    done = threading.Event()

    def handler(ev):
        got.append(ev)
        if len(got) >= events_wanted:
            done.set()

    chat.start(handler)
    done.wait(wait)
    chat.stop()
    return got


def test_receive_end_to_end_and_subscription_shape(http, clock):
    ws = FakeWs([welcome(), notification(chat_event())])
    chat = make(http, clock, connect=lambda url, t: ws)
    got = run_chat(chat)
    assert [e.text for e in got] == ['hello']
    sub = http.posts('/eventsub/subscriptions')[0][2]
    assert sub['json']['type'] == 'channel.chat.message'
    assert sub['json']['condition'] == {'broadcaster_user_id': '111', 'user_id': '222'}
    assert sub['json']['transport'] == {'method': 'websocket', 'session_id': 'sess1'}
    assert ws.closed


def test_duplicate_and_foreign_channel_events_dropped(http, clock):
    ws = FakeWs([welcome(), notification(chat_event(), 'n-1'), notification(chat_event(), 'n-1'),
                 notification(chat_event(broadcaster_user_id='999', message_id='m-2'), 'n-2'),
                 notification(chat_event(message_id='m-3'), 'n-3')])
    chat = make(http, clock, connect=lambda url, t: ws)
    got = run_chat(chat, events_wanted=2)
    assert [e.message_id for e in got] == ['m-1', 'm-3']


def test_handler_exception_does_not_stop_receiver(http, clock):
    ws = FakeWs([welcome(), notification(chat_event(), 'a'),
                 notification(chat_event(message_id='m-2'), 'b')])
    chat = make(http, clock, connect=lambda url, t: ws)
    seen = []
    done = threading.Event()

    def handler(ev):
        seen.append(ev.message_id)
        if len(seen) == 2:
            done.set()
        raise RuntimeError('consumer bug')

    chat.start(handler)
    done.wait(3)
    chat.stop()
    assert seen == ['m-1', 'm-2']


def test_keepalive_timeout_reconnects(http, clock):
    first = FakeWs([welcome(keepalive=10)])  # then silence: recv times out
    second = FakeWs([welcome(sid='sess2'), notification(chat_event())])
    conns = [first, second]
    chat = make(http, clock, connect=lambda url, t: conns.pop(0))
    got = run_chat(chat)
    assert got and first.closed
    assert len(http.posts('/eventsub/subscriptions')) == 2
    assert first.timeout == 10 + tw.KEEPALIVE_GRACE


def test_session_reconnect_follows_url_without_resubscribing(http, clock):
    url = 'wss://eventsub.wss.twitch.tv/ws?reconnect=1'
    reconnect = {'metadata': {'message_type': 'session_reconnect'},
                 'payload': {'session': {'reconnect_url': url}}}
    first = FakeWs([welcome(), reconnect])
    second = FakeWs([welcome(sid='sess2'), notification(chat_event())])
    urls = []

    def connect(u, t):
        urls.append(u)
        return second if len(urls) == 2 else first

    chat = make(http, clock, connect=connect)
    got = run_chat(chat)
    assert got and urls[1] == url
    assert first.closed
    assert len(http.posts('/eventsub/subscriptions')) == 1


def test_reconnect_to_foreign_host_refused(http, clock):
    evil = 'wss://attacker.example/ws'
    reconnect = {'metadata': {'message_type': 'session_reconnect'},
                 'payload': {'session': {'reconnect_url': evil}}}
    urls = []
    ws = FakeWs([welcome(), reconnect])

    def connect(u, t):
        urls.append(u)
        return ws if len(urls) == 1 else FakeWs([welcome(), notification(chat_event())])

    chat = make(http, clock, connect=connect)
    run_chat(chat)
    assert evil not in urls


def test_connect_failures_back_off_bounded_and_recover(http, clock):
    attempts = []

    def connect(u, t):
        attempts.append(u)
        if len(attempts) < 3:
            raise OSError('down')
        return FakeWs([welcome(), notification(chat_event())])

    chat = make(http, clock, connect=connect)
    got = run_chat(chat)
    assert got and len(attempts) == 3


def test_subscribe_refused_is_retried_not_fatal(http, clock):
    http.on('post', '/eventsub/subscriptions', Resp(403), Resp(202))
    conns = [FakeWs([welcome()]), FakeWs([welcome(), notification(chat_event())])]
    chat = make(http, clock, connect=lambda u, t: conns.pop(0))
    assert run_chat(chat)


def test_revocation_closes_session(http, clock):
    revoke = {'metadata': {'message_type': 'revocation'},
              'payload': {'subscription': {'status': 'authorization_revoked'}}}
    ws = FakeWs([welcome(), revoke])
    chat = make(http, clock, connect=lambda u, t: ws)
    chat.start(lambda e: None)
    for _ in range(100):
        if ws.closed:
            break
        threading.Event().wait(0.02)
    chat.stop()
    assert ws.closed
    assert chat._auth_failed_at is not None


def test_stop_joins_thread(http, clock):
    chat = make(http, clock, connect=lambda u, t: FakeWs([welcome()]))
    chat.start(lambda e: None)
    thread = chat._thread
    assert chat.stop() is True
    assert not thread.is_alive()


def test_login_channel_resolved_via_helix(http, clock, creds):
    creds['channel'] = 'FakeChannel'
    http.on('get', '/users', Resp(200, {'data': [{'id': '111', 'login': 'fakechannel'}]}))
    chat = make(http, clock)
    assert chat.send('fakechannel', 'hi') is True
    assert http.posts('/chat/messages')[0][2]['json']['broadcaster_id'] == '111'


# ===========================================================================
# Hardening round: hostile inputs, races and the protections they prove.
# ===========================================================================

def primed(http, clock, **kw):
    """A chat object ready for direct _dispatch calls, with its events collected."""
    chat = make(http, clock, **kw)
    got = []
    chat._handler = got.append
    chat._broadcaster_id = '111'
    chat._creds = dict(CREDS)
    return chat, got


def validate_calls(http):
    return [c for c in http.calls if c[1].endswith('/oauth2/validate')]


def refresh_tokens_used(http):
    return [c[2]['data']['refresh_token'] for c in http.posts('/oauth2/token')]


# ----- 1. shared chat -------------------------------------------------------

def test_shared_chat_message_from_another_channel_is_dropped(http, clock):
    chat, got = primed(http, clock)
    chat._dispatch(notification(chat_event(source_broadcaster_user_id='999',
                                           source_message_id='src-1')))
    assert got == []


@pytest.mark.parametrize('source', [None, '', '111'])
def test_message_from_the_configured_channel_is_delivered(http, clock, source):
    chat, got = primed(http, clock)
    chat._dispatch(notification(chat_event(source_broadcaster_user_id=source)))
    assert [e.message_id for e in got] == ['m-1']


def test_sends_pause_while_shared_chat_is_seen(http, clock):
    chat, got = primed(http, clock)
    assert chat.send('111', 'before') is True
    chat._dispatch(notification(chat_event(source_broadcaster_user_id='999')))
    clock.now += 2
    assert chat.send('111', 'during') is False
    assert len(http.posts('/chat/messages')) == 1
    clock.now += tw.SHARED_CHAT_QUIET + 1
    assert chat.send('111', 'after') is True


# ----- 2. token rotation, recovery, callback -------------------------------

def test_rotated_refresh_token_survives_transient_failure_and_recovery(http, clock):
    # R0 -> R1 works; the next refresh (R1) meets a 503; after the cooldown the
    # stored R0 must NOT replace the newer R1 held in memory.
    http.on('get', '/oauth2/validate', Resp(401), valid_body(), Resp(401), Resp(401),
            valid_body())
    http.on('post', '/oauth2/token',
            Resp(200, {'access_token': NEW_ACCESS, 'refresh_token': NEW_REFRESH}),
            Resp(503),
            Resp(200, {'access_token': 'ACCESS-3', 'refresh_token': 'REFRESH-3'}))
    chat = make(http, clock)
    assert chat.is_available() is True
    clock.now += tw.VALIDATE_INTERVAL + 1
    assert chat.is_available() is False
    clock.now += tw.AUTH_COOLDOWN + 1
    assert chat.is_available() is True
    assert refresh_tokens_used(http) == [REFRESH, NEW_REFRESH, NEW_REFRESH]


def test_operator_replacing_stored_credentials_is_adopted(http, clock, creds):
    http.on('get', '/oauth2/validate', Resp(401), valid_body())
    http.on('post', '/oauth2/token', Resp(400, {}))
    chat = make(http, clock)
    assert chat.is_available() is False
    creds['access_token'] = 'OPERATOR-ACCESS'
    creds['refresh_token'] = 'OPERATOR-REFRESH'
    clock.now += tw.AUTH_COOLDOWN + 1
    assert chat.is_available() is True
    assert validate_calls(http)[-1][2]['headers']['Authorization'] == 'OAuth OPERATOR-ACCESS'


def test_cooldown_recovery_invalidates_the_secret_cache(http, clock, monkeypatch):
    events = []
    monkeypatch.setattr(tw, 'reset_secret_cache', lambda: events.append('reset'))
    monkeypatch.setattr(tw, 'platform_secret',
                        lambda platform, key, default=None:
                        events.append('read') or CREDS.get(key, default))
    http.on('get', '/oauth2/validate', Resp(401), valid_body())
    http.on('post', '/oauth2/token', Resp(400, {}))
    chat = make(http, clock)
    assert chat.is_available() is False
    for _ in range(3):
        chat.is_available()  # inside the cooldown: no storm, no reset
    assert 'reset' not in events
    events.clear()
    clock.now += tw.AUTH_COOLDOWN + 1
    assert chat.is_available() is True
    assert events[0] == 'reset' and events.count('reset') == 1 and 'read' in events


def test_on_token_refresh_gets_the_replacement_pair(http, clock, caplog):
    seen = []
    http.on('get', '/oauth2/validate', Resp(401), valid_body())
    http.on('post', '/oauth2/token', Resp(200, {'access_token': NEW_ACCESS,
                                                 'refresh_token': NEW_REFRESH}))
    chat = make(http, clock, on_token_refresh=lambda a, r: seen.append((a, r)))
    with caplog.at_level(logging.DEBUG):
        assert chat.is_available() is True
    assert seen == [(NEW_ACCESS, NEW_REFRESH)]
    assert NEW_ACCESS not in caplog.text and NEW_REFRESH not in caplog.text


def test_on_token_refresh_not_called_when_refresh_fails(http, clock):
    seen = []
    http.on('get', '/oauth2/validate', Resp(401))
    http.on('post', '/oauth2/token', Resp(400, {}))
    chat = make(http, clock, on_token_refresh=lambda a, r: seen.append((a, r)))
    assert chat.is_available() is False
    assert seen == []


def test_on_token_refresh_exception_is_swallowed_and_logged_by_type(http, clock, caplog):
    def boom(access, refresh):
        raise RuntimeError(f'persist failed for {access} {refresh}')

    http.on('get', '/oauth2/validate', Resp(401), valid_body())
    http.on('post', '/oauth2/token', Resp(200, {'access_token': NEW_ACCESS,
                                                 'refresh_token': NEW_REFRESH}))
    chat = make(http, clock, on_token_refresh=boom)
    with caplog.at_level(logging.DEBUG):
        assert chat.is_available() is True
    assert 'RuntimeError' in caplog.text
    assert NEW_ACCESS not in caplog.text and NEW_REFRESH not in caplog.text


def test_refresh_without_a_new_refresh_token_keeps_the_current_one(http, clock):
    http.on('get', '/oauth2/validate', Resp(401), valid_body(), Resp(401), valid_body())
    http.on('post', '/oauth2/token', Resp(200, {'access_token': NEW_ACCESS}),
            Resp(200, {'access_token': 'ACCESS-3'}))
    chat = make(http, clock)
    assert chat.is_available() is True
    clock.now += tw.VALIDATE_INTERVAL + 1
    assert chat.is_available() is True
    assert refresh_tokens_used(http) == [REFRESH, REFRESH]


# ----- 3. stop / start lifecycle --------------------------------------------

def wait_for(predicate, seconds=3.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_stop_keeps_a_stuck_worker_and_start_refuses_a_second(http, clock, caplog):
    entered = threading.Event()
    release = threading.Event()
    connects = []
    sockets = []

    def connect(url, timeout):
        connects.append(url)
        entered.set()
        release.wait(5)
        sockets.append(FakeWs([welcome(), notification(chat_event())]))
        return sockets[-1]

    got = []
    chat = make(http, clock, connect=connect, stop_timeout=0.05)
    chat.start(got.append)
    assert entered.wait(3)
    worker = chat._thread
    with caplog.at_level(logging.WARNING):
        assert chat.stop() is False
    assert 'incomplete' in caplog.text
    assert chat._thread is worker and worker.is_alive()
    chat.start(got.append)  # must not spawn a second receiver
    assert chat._thread is worker
    assert len(connects) == 1
    release.set()
    assert wait_for(lambda: not worker.is_alive())
    # Cancelled right after connect: nothing read, never subscribed or delivered.
    assert sockets[0].closed and sockets[0].q.qsize() == 2
    assert not http.posts('/eventsub/subscriptions')
    assert got == []
    chat.start(got.append)  # the old worker is gone, so this is allowed
    assert chat._thread is not worker
    chat.stop()


def test_stop_reports_a_clean_shutdown(http, clock):
    chat = make(http, clock, connect=lambda u, t: FakeWs([welcome()]))
    chat.start(lambda e: None)
    assert chat.stop() is True


def test_stop_between_welcome_and_subscribe_never_subscribes(http, clock):
    chat = make(http, clock)

    class StoppingWs(FakeWs):
        def recv(self):
            frame = super().recv()
            chat._stop.set()  # stop() lands right after the welcome frame
            return frame

    chat._stop.clear()
    chat._broadcaster_id = '111'
    chat._ensure_auth()
    chat._connect = lambda u, t: StoppingWs([welcome()])
    chat._cycle()
    assert not http.posts('/eventsub/subscriptions')


# ----- 4. frames bounded by bytes, before allocation --------------------------

def test_frame_limit_counts_bytes_not_characters(http, clock):
    emoji = json.dumps({'x': '\U0001F600' * 300000}, ensure_ascii=False)
    assert len(emoji) < tw.MAX_FRAME_BYTES < len(emoji.encode('utf-8'))
    assert make(http, clock)._recv(FakeWs([emoji]), 1) == {}


def test_frame_exactly_at_the_limit_is_accepted(http, clock):
    pad = tw.MAX_FRAME_BYTES - len(json.dumps({'p': ''}))
    raw = json.dumps({'p': 'a' * pad})
    assert len(raw.encode('utf-8')) == tw.MAX_FRAME_BYTES
    assert make(http, clock)._recv(FakeWs([raw]), 1) == {'p': 'a' * pad}


def test_deeply_nested_json_is_dropped_not_fatal(http, clock):
    assert make(http, clock)._recv(FakeWs(['[' * 200000]), 1) == {}


def ws_frame(payload, opcode=1, fin=True):
    """One unmasked server-to-client frame, hand built."""
    first = (0x80 if fin else 0) | opcode
    n = len(payload)
    if n < 126:
        head = struct.pack('!BB', first, n)
    elif n < 65536:
        head = struct.pack('!BBH', first, 126, n)
    else:
        head = struct.pack('!BBQ', first, 127, n)
    return head + payload


@pytest.fixture
def socket_ws():
    a, b = socket.socketpair()
    a.settimeout(2)
    ws = tw._BoundedWebSocket()
    ws.sock = a
    ws.connected = True
    yield ws, b
    for s in (a, b):
        s.close()


def test_real_frame_declaring_a_huge_length_is_refused_before_reading_it(socket_ws):
    ws, peer = socket_ws
    # Header declares 8 MiB; only 16 payload bytes ever arrive. A reader that
    # trusts the header would block waiting for the rest and time out instead.
    peer.sendall(struct.pack('!BBQ', 0x81, 127, 8 * 1024 * 1024) + b'x' * 16)
    with pytest.raises(tw.FrameTooLarge):
        ws.recv()


def test_real_fragmented_message_is_bounded_in_aggregate(socket_ws):
    ws, peer = socket_ws
    chunk = b'a' * (400 * 1024)  # each fragment is under the cap, three are over it
    data = (ws_frame(chunk, opcode=1, fin=False) + ws_frame(chunk, opcode=0, fin=False)
            + ws_frame(chunk, opcode=0, fin=True))
    sender = threading.Thread(target=lambda: _quiet_sendall(peer, data), daemon=True)
    sender.start()
    with pytest.raises(tw.FrameTooLarge):
        ws.recv()


def _quiet_sendall(sock, data):
    try:
        sock.sendall(data)
    except OSError:
        pass  # the reader gave up and the pair was closed: expected


def test_real_ordinary_and_fragmented_messages_still_work(socket_ws):
    ws, peer = socket_ws
    peer.sendall(ws_frame(b'hello'))
    assert ws.recv() == 'hello'
    peer.sendall(ws_frame(b'par', opcode=1, fin=False) + ws_frame(b'ts', opcode=0, fin=True))
    assert ws.recv() == 'parts'


GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


class LoopbackWs:
    """A loopback-only websocket server; `script(conn, path, accept)` drives it."""

    def __init__(self, script):
        self.script = script
        self.accepted = 0
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(5)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self._done = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._done:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.accepted += 1
            conn.settimeout(3)
            try:
                request = b''
                while b'\r\n\r\n' not in request:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    request += chunk
                lines = request.decode('latin-1').split('\r\n')
                path = lines[0].split(' ')[1] if lines and ' ' in lines[0] else ''
                key = next((ln.split(':', 1)[1].strip() for ln in lines
                            if ln.lower().startswith('sec-websocket-key')), '')
                accept = base64.b64encode(
                    hashlib.sha1((key + GUID).encode()).digest()).decode()
                self.script(conn, path, accept)
            except OSError:
                # The client may hang up mid-handshake in the redirect and abort tests.
                pass
            finally:
                conn.close()

    def close(self):
        self._done = True
        self.sock.close()


def _upgrade(conn, accept):
    conn.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
                  'Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n' % accept).encode())


@pytest.fixture
def no_proxy(monkeypatch):
    for var in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy',
                'ALL_PROXY'):
        monkeypatch.delenv(var, raising=False)


def test_default_connect_returns_the_bounded_reader(no_proxy):
    def script(conn, path, accept):
        _upgrade(conn, accept)
        conn.sendall(struct.pack('!BBQ', 0x81, 127, 8 * 1024 * 1024) + b'x' * 16)
        conn.settimeout(3)
        try:
            conn.recv(1)
        except OSError:
            # The peer closing first is expected; there is nothing to clean up.
            pass

    server = LoopbackWs(script)
    try:
        ws = tw._default_connect(f'ws://127.0.0.1:{server.port}/ws', 2)
        with pytest.raises(tw.FrameTooLarge):
            ws.recv()
        ws.close()
    finally:
        server.close()


def test_handshake_redirect_is_not_followed(no_proxy):
    target = LoopbackWs(lambda conn, path, accept: _upgrade(conn, accept))

    def script(conn, path, accept):
        conn.sendall(('HTTP/1.1 302 Found\r\nLocation: ws://127.0.0.1:%d/ws\r\n'
                      'Content-Length: 0\r\n\r\n' % target.port).encode())

    origin = LoopbackWs(script)
    try:
        with pytest.raises(Exception):
            tw._default_connect(f'ws://127.0.0.1:{origin.port}/ws', 2)
        time.sleep(0.5)
        assert origin.accepted == 1
        assert target.accepted == 0
    finally:
        origin.close()
        target.close()


# ----- 6. token expiry ------------------------------------------------------

def test_known_expiry_ends_availability_even_if_revalidation_is_down(http, clock):
    http.on('get', '/oauth2/validate', valid_body(expires_in=1000), Resp(500))
    chat = make(http, clock)
    assert chat.is_available() is True
    clock.now += 1001
    assert chat.is_available() is False


def test_token_is_revalidated_when_expiry_is_near(http, clock):
    http.on('get', '/oauth2/validate', valid_body(expires_in=1000))
    chat = make(http, clock)
    assert chat.is_available() is True
    clock.now += 150  # 850 s left, inside the refresh margin, far inside the hour
    assert chat.is_available() is True
    assert len(validate_calls(http)) == 2


def test_stale_tolerance_never_outlives_the_known_expiry(http, clock):
    http.on('get', '/oauth2/validate', valid_body(expires_in=2000), Resp(500))
    chat = make(http, clock)
    assert chat.is_available() is True
    clock.now += 1900
    assert chat.is_available() is True   # revalidation is down, the token still has time
    clock.now += 101
    assert chat.is_available() is False  # past the known deadline


def test_stale_tolerance_is_capped_for_long_lived_tokens(http, clock):
    http.on('get', '/oauth2/validate', valid_body(expires_in=10 ** 6), Resp(500))
    chat = make(http, clock)
    assert chat.is_available() is True
    clock.now += tw.VALIDATE_INTERVAL + 1
    assert chat.is_available() is True
    clock.now += tw.VALIDATE_INTERVAL
    assert chat.is_available() is False


# ----- 7. replacement token is staged until validated -------------------------

def test_sender_never_posts_with_a_replacement_that_fails_validation(http, clock):
    at_gate = threading.Event()
    go = threading.Event()

    class Gated(TwitchChat):
        def _ensure_auth(self, force=False):
            ok = super()._ensure_auth(force)
            if not force and threading.current_thread().name == 'sender':
                at_gate.set()
                assert go.wait(5)
            return ok

    http.on('get', '/oauth2/validate', valid_body(), Resp(401), valid_body(user_id='999'))
    http.on('post', '/oauth2/token', Resp(200, {'access_token': NEW_ACCESS,
                                                 'refresh_token': NEW_REFRESH}))
    chat = Gated(http=http, connect=lambda u, t: FakeWs(), clock=clock,
                 backoff_base=0.001, long_rest=0.01, auth_poll=0.01)
    assert chat.is_available() is True
    result = []
    sender = threading.Thread(name='sender', target=lambda: result.append(chat.send('111', 'hi')))
    sender.start()
    assert at_gate.wait(3)
    assert chat._ensure_auth(force=True) is False  # refresh publishes a wrong-user token
    go.set()
    sender.join(5)
    assert result == [False]
    assert http.posts('/chat/messages') == []


class BlockingResp(Resp):
    """A validate response that holds the validation open until released."""

    def __init__(self, body, entered, release):
        super().__init__(200, body)
        self.entered, self.release = entered, release

    def json(self):
        self.entered.set()
        assert self.release.wait(5)
        return self._body


def test_sender_blocks_while_a_replacement_is_being_validated(http, clock):
    entered, release, go = threading.Event(), threading.Event(), threading.Event()
    at_gate = threading.Event()

    class Gated(TwitchChat):
        def _ensure_auth(self, force=False):
            ok = super()._ensure_auth(force)
            if not force and threading.current_thread().name == 'sender':
                at_gate.set()
                assert go.wait(5)
            return ok

    wrong_user = valid_body(user_id='999').json()
    http.on('get', '/oauth2/validate', valid_body(), Resp(401),
            BlockingResp(wrong_user, entered, release))
    http.on('post', '/oauth2/token', Resp(200, {'access_token': NEW_ACCESS,
                                                 'refresh_token': NEW_REFRESH}))
    chat = Gated(http=http, connect=lambda u, t: FakeWs(), clock=clock,
                 backoff_base=0.001, long_rest=0.01, auth_poll=0.01)
    assert chat.is_available() is True
    result = []
    sender = threading.Thread(name='sender', target=lambda: result.append(chat.send('111', 'hi')))
    sender.start()
    assert at_gate.wait(3)
    refresher = threading.Thread(target=lambda: chat._ensure_auth(force=True))
    refresher.start()
    assert entered.wait(3)   # the replacement token exists and validation is pending
    go.set()                 # the sender now goes for its credentials
    sender.join(0.3)
    assert sender.is_alive() or http.posts('/chat/messages') == []
    release.set()
    refresher.join(5)
    sender.join(5)
    assert result == [False]
    assert http.posts('/chat/messages') == []


# ----- 8. websocket URL and redirects -----------------------------------------

@pytest.mark.parametrize('url', [
    'wss://eventsub.wss.twitch.tv/ws?keepalive_timeout_seconds=30',
    'wss://eventsub.wss.twitch.tv?reconnect=abc',
    'wss://eventsub.wss.twitch.tv:443/ws?x=1',
    'wss://EventSub.WSS.Twitch.TV/ws',
])
def test_reconnect_url_accepts_the_documented_forms(url):
    assert TwitchChat._reconnect_url_ok(url) is True


@pytest.mark.parametrize('url', [
    'ws://eventsub.wss.twitch.tv/ws',                      # not TLS
    'https://eventsub.wss.twitch.tv/ws',
    'wss://evil.twitch.tv/ws',                             # some other twitch.tv host
    'wss://twitch.tv/ws',
    'wss://eventsub.wss.twitch.tv.evil.example/ws',
    'wss://eventsub.wss.twitch.tv.@evil.example/ws',
    'wss://user:pw@eventsub.wss.twitch.tv/ws',             # userinfo
    'wss://user@eventsub.wss.twitch.tv/ws',
    'wss://evil.example@eventsub.wss.twitch.tv/ws',
    'wss://eventsub.wss.twitch.tv:444/ws',                 # other port
    'wss://eventsub.wss.twitch.tv:abc/ws',                 # non-numeric port
    'wss://eventsub.wss.twitch.tv:/ws#frag',
    'wss://eventsub.wss.twitch.tv/other',
    'wss://eventsub.wss.twitch.tv./ws',
    '', None, 12345,
])
def test_reconnect_url_rejects_everything_else(url):
    assert TwitchChat._reconnect_url_ok(url) is False


def test_plain_ws_reconnect_is_never_dialled(http, clock):
    bad = 'ws://eventsub.wss.twitch.tv/ws'
    reconnect = {'metadata': {'message_type': 'session_reconnect'},
                 'payload': {'session': {'reconnect_url': bad}}}
    urls = []

    def connect(u, t):
        urls.append(u)
        return FakeWs([welcome(), reconnect]) if len(urls) == 1 else FakeWs([welcome()])

    chat = make(http, clock, connect=connect)
    chat.start(lambda e: None)
    assert wait_for(lambda: len(urls) >= 2)
    chat.stop()
    assert bad not in urls


# ----- 9. outgoing text -------------------------------------------------------

def posted(http):
    return [c[2]['json']['message'] for c in http.posts('/chat/messages')]


@pytest.mark.parametrize('text', [
    '\n /ban victim', '/ban victim', '.ban victim', '  .timeout x 600', '/me waves',
    '\x00/ban victim', '\x01.ban victim', '\x1b/ban victim', '​/ban victim',
    '\x85\t/ban victim',
])
def test_command_prefixed_messages_are_refused(http, clock, text, caplog):
    chat = make(http, clock)
    with caplog.at_level(logging.DEBUG):
        assert chat.send('111', text) is False
    assert http.posts('/chat/messages') == []
    assert 'victim' not in caplog.text and 'ban' not in caplog.text.lower().replace('banned', '')


def test_command_prefix_allowed_only_on_explicit_opt_in(http, clock):
    chat = make(http, clock, allow_command_prefix=True)
    assert chat.send('111', '/me waves') is True
    assert posted(http) == ['/me waves']


@pytest.mark.parametrize('text,expected', [
    ('a\x00b\x01c\x1bd\x7fe', 'abcde'),
    ('x\x85y\tz', 'x y z'),
    ('tab\there\nand\r\nnewlines', 'tab here and newlines'),
    ('c1\x80\x9fcontrols', 'c1controls'),
    ('hello. world / fine', 'hello. world / fine'),
])
def test_control_characters_are_removed_and_whitespace_collapsed(http, clock, text, expected):
    assert make(http, clock).send('111', text) is True
    assert posted(http) == [expected]


def test_message_that_is_only_controls_is_not_sent(http, clock):
    assert make(http, clock).send('111', '\x00\x01\x1b') is False
    assert http.posts('/chat/messages') == []


# ----- 10. replay window and dedupe -------------------------------------------

def test_notification_older_than_the_replay_window_is_dropped(http, clock):
    chat, got = primed(http, clock)
    chat._dispatch(notification(chat_event(), 'old', ts=iso(-(tw.REPLAY_WINDOW + 5))))
    chat._dispatch(notification(chat_event(message_id='m-2'), 'new', ts=iso(-(tw.REPLAY_WINDOW - 30))))
    assert [e.message_id for e in got] == ['m-2']


@pytest.mark.parametrize('ts', [None, '', 'yesterday', '2023-13-45T99:00:00Z', 12345])
def test_notification_without_a_usable_timestamp_is_dropped(http, clock, ts):
    chat, got = primed(http, clock)
    frame = notification(chat_event())
    if ts is None:
        del frame['metadata']['message_timestamp']
    else:
        frame['metadata']['message_timestamp'] = ts
    chat._dispatch(frame)
    assert got == []


def test_replay_after_the_id_cache_wrapped_is_still_refused(http, clock):
    chat, got = primed(http, clock)
    chat._dispatch(notification(chat_event(), 'first'))
    for i in range(tw.SEEN_IDS + 10):  # push 'first' out of the size-bounded cache
        chat._dispatch(notification(chat_event(message_id=f'x{i}'), f'filler-{i}'))
    assert 'first' not in chat._seen
    n = len(got)
    chat._dispatch(notification(chat_event(), 'first', ts=iso(-(tw.REPLAY_WINDOW + 60))))
    assert len(got) == n


def test_id_cache_is_bounded_by_size_and_by_time(http, clock):
    chat, got = primed(http, clock)
    for i in range(tw.SEEN_IDS + 50):
        chat._dispatch(notification(chat_event(message_id=f'm{i}'), f'id-{i}'))
    assert len(chat._seen) <= tw.SEEN_IDS
    clock.now += tw.REPLAY_WINDOW + 1
    chat._dispatch(notification(chat_event(message_id='late'), 'late-id'))
    assert list(chat._seen) == ['late-id']


def test_duplicate_inside_the_window_is_dropped(http, clock):
    chat, got = primed(http, clock)
    chat._dispatch(notification(chat_event(), 'same'))
    chat._dispatch(notification(chat_event(), 'same'))
    assert len(got) == 1


# ----- 11. sanitized exceptions carry no context ------------------------------

def test_recv_failure_has_no_context_or_cause(http, clock):
    ws = FakeWs([RuntimeError('SYNTHETIC-BEARER-SECRET')])
    with pytest.raises(tw._Transient) as info:
        make(http, clock)._recv(ws, 1)
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert 'SYNTHETIC-BEARER-SECRET' not in repr(info.value)


def test_oversize_frame_failure_has_no_context(http, clock):
    class Oversize(FakeWs):
        def recv(self):
            raise tw.FrameTooLarge('SYNTHETIC-BEARER-SECRET')

    with pytest.raises(tw._Transient) as info:
        make(http, clock)._recv(Oversize(), 1)
    assert info.value.__context__ is None and info.value.__cause__ is None


def test_follow_reconnect_failure_has_no_context(http, clock):
    chat = make(http, clock)
    new_ws = FakeWs([RuntimeError('SYNTHETIC-BEARER-SECRET')])
    chat._connect = lambda u, t: new_ws
    with pytest.raises(tw._Transient) as info:
        chat._follow_reconnect(FakeWs(), 'wss://eventsub.wss.twitch.tv/ws')
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert new_ws.closed


@pytest.mark.parametrize('value', ['SYNTHETIC-BEARER-SECRET', 10 ** 9, 0, -5, [], None])
def test_garbage_keepalive_in_welcome_falls_back_to_the_default(http, clock, value):
    bad = welcome()
    bad['payload']['session']['keepalive_timeout_seconds'] = value
    _, keepalive, _ = make(http, clock)._welcome(FakeWs([bad]))
    assert keepalive == tw.KEEPALIVE_SECONDS


# ----- 12. protections that were unproven -------------------------------------

def test_every_credential_bearing_request_refuses_redirects(http, clock, creds):
    creds['channel'] = 'FakeChannel'
    http.on('get', '/users', Resp(200, {'data': [{'id': '111', 'login': 'fakechannel'}]}))
    http.on('get', '/oauth2/validate', Resp(401), valid_body())
    http.on('post', '/oauth2/token', Resp(200, {'access_token': NEW_ACCESS,
                                                 'refresh_token': NEW_REFRESH}))
    chat = make(http, clock)
    assert chat.send('fakechannel', 'hi') is True
    chat._subscribe('sess-1')
    urls = [c[1] for c in http.calls]
    for suffix in ('/oauth2/validate', '/oauth2/token', '/users', '/chat/messages',
                   '/eventsub/subscriptions'):
        assert any(u.endswith(suffix) for u in urls), suffix
    for method, url, kw in http.calls:
        assert kw.get('allow_redirects') is False, url


class RaisingStatus:
    """A response whose status access raises, to reach the outer exception handlers."""

    headers = {}

    @property
    def status_code(self):
        raise RuntimeError(f'leak Bearer {ACCESS} {CLIENT_SECRET}')

    def json(self):
        return {}


def test_outer_send_handler_logs_type_only_and_is_actually_reached(http, clock, caplog):
    http.on('post', '/chat/messages', RaisingStatus())
    chat = make(http, clock)
    with caplog.at_level(logging.DEBUG):
        assert chat.send('111', 'hi') is False
    assert len(http.posts('/chat/messages')) == 1  # the scripted failure was reached
    assert 'RuntimeError' in caplog.text
    for secret in SECRETS:
        assert secret not in caplog.text


def test_outer_availability_handler_logs_type_only(http, clock, caplog):
    http.on('get', '/oauth2/validate', RaisingStatus())
    with caplog.at_level(logging.DEBUG):
        assert make(http, clock).is_available() is False
    assert 'RuntimeError' in caplog.text
    for secret in SECRETS:
        assert secret not in caplog.text


def test_receive_loop_logs_type_only(http, clock, caplog):
    def connect(url, timeout):
        raise OSError(f'refused with {ACCESS} {CLIENT_SECRET}')

    chat = make(http, clock, connect=connect)
    with caplog.at_level(logging.DEBUG):
        chat.start(lambda e: None)
        assert wait_for(lambda: 'OSError' in caplog.text)
        chat.stop()
    for secret in SECRETS:
        assert secret not in caplog.text


def test_wrong_subscription_type_is_ignored(http, clock):
    chat, got = primed(http, clock)
    chat._dispatch(notification(chat_event(), 'a', sub_type='channel.follow'))
    chat._dispatch(notification(chat_event(), 'b', sub_type=''))
    assert got == []
    chat._dispatch(notification(chat_event(message_id='ok'), 'c'))
    assert [e.message_id for e in got] == ['ok']


class BarrierDeque(collections.deque):
    """Makes two concurrent limiter checks overlap, if nothing serialises them."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.barrier = threading.Barrier(2)

    def __len__(self):
        size = super().__len__()  # read first: both racers must see the same state
        if threading.current_thread().name.startswith('racer'):
            try:
                self.barrier.wait(timeout=0.4)
            except threading.BrokenBarrierError:
                # The other party may already have left; the barrier is only a rendezvous.
                pass
        return size


def test_send_limiter_is_serialised(http, clock):
    chat = make(http, clock)
    assert chat.is_available() is True
    chat._broadcaster_id = '111'
    chat._sent_times = BarrierDeque()
    results = []
    threads = [threading.Thread(name=f'racer-{i}',
                                target=lambda: results.append(chat.send('111', 'hi')))
               for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    # Two sends at the same instant: the 1.5 s spacing allows exactly one.
    assert sorted(results) == [False, True]
    assert len(http.posts('/chat/messages')) == 1
