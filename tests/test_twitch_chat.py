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
import websocket
from websocket._abnf import frame_buffer

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
    h.on('get', '/shared_chat/session', Resp(200, {'data': []}))
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
    assert len(chat_subs(http)) == 2
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
    assert len(chat_subs(http)) == 1


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
    chat.stop()
    assert chat.stopped_cleanly is True
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


def chat_subs(http):
    return [c for c in http.posts('/eventsub/subscriptions')
            if c[2]['json']['type'] == 'channel.chat.message']


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
        chat.stop()  # drains the persistence queue
    assert seen == [(NEW_ACCESS, NEW_REFRESH)]
    assert NEW_ACCESS not in caplog.text and NEW_REFRESH not in caplog.text


def test_on_token_refresh_not_called_when_refresh_fails(http, clock):
    seen = []
    http.on('get', '/oauth2/validate', Resp(401))
    http.on('post', '/oauth2/token', Resp(400, {}))
    chat = make(http, clock, on_token_refresh=lambda a, r: seen.append((a, r)))
    assert chat.is_available() is False
    chat.stop()
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
        chat.stop()
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
        chat.stop()
    assert chat.stopped_cleanly is False
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
    chat.stop()
    assert chat.stopped_cleanly is True


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
                   '/eventsub/subscriptions', '/shared_chat/session'):
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


# ===========================================================================
# Second hardening round.
# ===========================================================================

@pytest.fixture(autouse=True)
def fresh_reader_probe(monkeypatch):
    """The bounded-reader compatibility probe is cached per process; start clean."""
    monkeypatch.setattr(tw, '_reader_cache', None)


def session_calls(http):
    return [c for c in http.calls if c[1].endswith('/shared_chat/session')]


def in_session():
    return Resp(200, {'data': [{'session_id': 's', 'host_broadcaster_id': '111',
                                'participants': [{'broadcaster_id': '111'},
                                                 {'broadcaster_id': '999'}]}]})


NOT_IN_SESSION = Resp(200, {'data': []})


class Unparseable(Resp):
    def json(self):
        raise ValueError('not json')


# ----- A. every send asks Twitch about shared chat, right before the POST -------

def test_active_session_refuses_the_send(http, clock, caplog):
    http.on('get', '/shared_chat/session', in_session())
    chat = make(http, clock)
    with caplog.at_level(logging.DEBUG):
        assert chat.send('111', 'hi') is False
    assert http.posts('/chat/messages') == []
    assert 'shared chat is active or could not be ruled out' in caplog.text


def test_no_session_sends(http, clock):
    chat = make(http, clock)
    assert chat.send('111', 'hi') is True
    assert session_calls(http)[0][2]['params'] == {'broadcaster_id': '111'}
    assert session_calls(http)[0][2]['headers']['Authorization'] == f'Bearer {ACCESS}'


@pytest.mark.parametrize('answer', [
    Resp(500), Resp(404), Resp(403), RuntimeError('SYNTHETIC-SECRET'), TimeoutError('slow'),
    Resp(200, {'unexpected': True}), Resp(200, {'data': 'nope'}), Resp(200, {'data': None}),
    Resp(200, [1]), Unparseable(200),
])
def test_unknown_answers_refuse_the_send(http, clock, answer):
    http.on('get', '/shared_chat/session', answer)
    assert make(http, clock).send('111', 'hi') is False
    assert http.posts('/chat/messages') == []


def test_the_check_happens_on_every_send_and_right_before_the_post(http, clock):
    http.on('get', '/shared_chat/session', NOT_IN_SESSION, in_session(), NOT_IN_SESSION)
    chat = make(http, clock)
    results = []
    for _ in range(3):
        clock.now += 2
        results.append(chat.send('111', 'hi'))
    assert results == [True, False, True]
    assert len(session_calls(http)) == 3
    order = [c[1].rsplit('/', 1)[-1] for c in http.calls
             if c[1].endswith(('/session', '/messages'))]
    assert order == ['session', 'messages', 'session', 'session', 'messages']


def test_a_401_retry_asks_again_before_the_second_post(http, clock):
    http.on('get', '/oauth2/validate', valid_body(), Resp(401), valid_body())
    http.on('post', '/oauth2/token', Resp(200, {'access_token': NEW_ACCESS,
                                                 'refresh_token': NEW_REFRESH}))
    http.on('post', '/chat/messages', Resp(401), Resp(200, {'data': [{'is_sent': True}]}))
    http.on('get', '/shared_chat/session', NOT_IN_SESSION, in_session())
    chat = make(http, clock)
    assert chat.send('111', 'hi') is False   # a session began while the token was refreshed
    assert len(session_calls(http)) == 2
    assert len(http.posts('/chat/messages')) == 1


def test_a_401_retry_that_stays_clear_posts_again(http, clock):
    http.on('get', '/oauth2/validate', valid_body(), Resp(401), valid_body())
    http.on('post', '/oauth2/token', Resp(200, {'access_token': NEW_ACCESS,
                                                 'refresh_token': NEW_REFRESH}))
    http.on('post', '/chat/messages', Resp(401), Resp(200, {'data': [{'is_sent': True}]}))
    chat = make(http, clock)
    assert chat.send('111', 'hi') is True
    assert len(session_calls(http)) == 2 and len(http.posts('/chat/messages')) == 2
    assert session_calls(http)[1][2]['headers']['Authorization'] == f'Bearer {NEW_ACCESS}'


def test_a_401_on_the_check_gets_one_refresh_and_then_refuses(http, clock):
    http.on('get', '/oauth2/validate', valid_body(), Resp(401), valid_body())
    http.on('post', '/oauth2/token', Resp(200, {'access_token': NEW_ACCESS,
                                                 'refresh_token': NEW_REFRESH}))
    http.on('get', '/shared_chat/session', Resp(401))
    chat = make(http, clock)
    assert chat.send('111', 'hi') is False
    assert len(session_calls(http)) == 2
    assert len(http.posts('/oauth2/token')) == 1
    assert http.posts('/chat/messages') == []


def test_a_401_on_the_check_that_one_refresh_fixes_sends(http, clock):
    http.on('get', '/oauth2/validate', valid_body(), Resp(401), valid_body())
    http.on('post', '/oauth2/token', Resp(200, {'access_token': NEW_ACCESS,
                                                 'refresh_token': NEW_REFRESH}))
    http.on('get', '/shared_chat/session', Resp(401), NOT_IN_SESSION)
    chat = make(http, clock)
    assert chat.send('111', 'hi') is True


def test_check_and_post_are_serialised(http, clock, monkeypatch):
    monkeypatch.setattr(tw, 'SEND_MIN_INTERVAL', 0.0)
    entered, release = threading.Event(), threading.Event()

    class Held(Resp):
        def json(self):
            entered.set()
            assert release.wait(5)
            return self._body

    http.on('get', '/shared_chat/session', Held(200, {'data': []}), NOT_IN_SESSION)
    chat = make(http, clock)
    assert chat.is_available() is True
    first = threading.Thread(target=lambda: chat.send('111', 'one'), daemon=True)
    second = threading.Thread(target=lambda: chat.send('111', 'two'), daemon=True)
    first.start()
    assert entered.wait(3)
    second.start()
    time.sleep(0.3)
    assert len(session_calls(http)) == 1       # the second send waits for the first
    assert http.posts('/chat/messages') == []
    release.set()
    first.join(5)
    second.join(5)
    assert len(session_calls(http)) == 2 and len(http.posts('/chat/messages')) == 2


def test_no_shared_chat_subscriptions_and_no_background_polling(http, clock):
    chat = make(http, clock, connect=lambda u, t: FakeWs([welcome(), notification(chat_event())]))
    chat.start(lambda e: None)
    assert wait_for(lambda: http.posts('/eventsub/subscriptions'))
    time.sleep(0.2)
    chat.stop()
    assert {c[2]['json']['type'] for c in http.posts('/eventsub/subscriptions')} == \
        {'channel.chat.message'}
    assert session_calls(http) == []
    for name in ('SHARED_CHAT_TYPES', 'SHARED_CHAT_QUIET'):
        assert not hasattr(tw, name)


def test_a_foreign_source_message_does_not_change_the_send_decision(http, clock):
    chat, got = primed(http, clock)
    chat._dispatch(notification(chat_event(source_broadcaster_user_id='999')))
    assert got == []
    assert chat.send('111', 'hi') is True     # only Twitch's own answer decides


# ----- B. refresh persistence: one latest-wins slot ------------------------------

def refreshing(http, clock, callback, rounds=3):
    http.on('get', '/oauth2/validate', valid_body(), *([Resp(401), valid_body()] * rounds))
    http.on('post', '/oauth2/token', *[Resp(200, {'access_token': f'A{i}', 'refresh_token': f'R{i}'})
                                      for i in range(1, rounds + 1)])
    return make(http, clock, on_token_refresh=callback)


def run_in_thread(fn, seconds=3.0):
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    t.join(seconds)
    return not t.is_alive()


def test_blocked_callback_never_blocks_and_the_latest_pair_arrives(http, clock):
    gate, entered, seen = threading.Event(), threading.Event(), []

    def callback(access, refresh):
        entered.set()
        assert gate.wait(5)
        seen.append((access, refresh))

    chat = refreshing(http, clock, callback)
    assert chat.is_available() is True
    for _ in range(3):
        assert run_in_thread(lambda: chat._ensure_auth(force=True))  # never waits for the callback
    assert entered.wait(3) and seen == []
    clock.now += 2
    assert run_in_thread(lambda: chat.send('111', 'hi'))  # the auth lock is free
    gate.set()
    assert wait_for(lambda: len(seen) == 2)
    # The first pair was already in flight; the second was overwritten by the third.
    assert seen == [('A1', 'R1'), ('A3', 'R3')]
    chat.stop()
    assert chat.stopped_cleanly is True


def test_the_slot_never_grows_behind_a_blocked_callback(http, clock):
    gate, entered = threading.Event(), threading.Event()

    def callback(access, refresh):
        entered.set()
        gate.wait(5)

    chat = refreshing(http, clock, callback, rounds=40)
    chat.is_available()
    chat._ensure_auth(force=True)
    assert entered.wait(3)
    for _ in range(39):
        chat._ensure_auth(force=True)
    assert chat._cb_pending == ('A40', 'R40')   # one pair, the newest
    gate.set()
    chat.stop()


def test_an_older_pair_is_never_delivered_after_a_newer_one(http, clock):
    seen = []
    chat = refreshing(http, clock, lambda a, r: seen.append(a), rounds=12)
    chat.is_available()
    for _ in range(12):
        chat._ensure_auth(force=True)
    chat.stop()
    numbers = [int(a[1:]) for a in seen]
    assert numbers == sorted(numbers) and numbers[-1] == 12


def test_callback_runs_on_one_dispatcher_thread_off_the_caller(http, clock):
    names = []
    chat = refreshing(http, clock, lambda a, r: names.append(threading.current_thread().name))
    chat.is_available()
    chat._ensure_auth(force=True)
    chat.stop()
    assert len(names) == 1 and names[0] != threading.current_thread().name


def test_a_failing_callback_does_not_stop_later_ones(http, clock):
    seen, first_done = [], threading.Event()

    def callback(access, refresh):
        seen.append(access)
        if access == 'A1':
            first_done.set()
            raise RuntimeError('persist failed')

    chat = refreshing(http, clock, callback)
    chat.is_available()
    chat._ensure_auth(force=True)
    assert first_done.wait(3)
    chat._ensure_auth(force=True)
    chat.stop()
    assert seen == ['A1', 'A2']


def test_base_exception_from_the_callback_is_contained(http, clock, caplog, capsys):
    seen, first_done = [], threading.Event()

    def callback(access, refresh):
        seen.append(access)
        if access == 'A1':
            first_done.set()
            raise BaseException(f'{access} {refresh} {NEW_REFRESH}')

    chat = refreshing(http, clock, callback)
    with caplog.at_level(logging.DEBUG):
        chat.is_available()
        chat._ensure_auth(force=True)
        assert first_done.wait(3)
        assert wait_for(lambda: chat._dispatcher is not None and chat._cb_pending is None)
        assert chat._dispatcher.is_alive()               # the dispatcher survived
        chat._ensure_auth(force=True)
        chat.stop()
    assert seen == ['A1', 'A2']
    captured = capsys.readouterr()
    for blob in (caplog.text, captured.err, captured.out):
        assert 'A1' not in blob and 'R1' not in blob and NEW_REFRESH not in blob
    assert 'BaseException' in caplog.text


def test_a_dead_dispatcher_is_restarted_by_the_next_refresh(http, clock):
    seen = []
    chat = refreshing(http, clock, lambda a, r: seen.append(a))
    chat.is_available()
    corpse = threading.Thread(target=lambda: None)
    corpse.start()
    corpse.join()
    chat._dispatcher = corpse
    chat._ensure_auth(force=True)
    assert wait_for(lambda: seen == ['A1'])
    assert chat._dispatcher is not corpse
    chat.stop()


def test_stop_reports_a_stuck_callback(http, clock, caplog):
    gate, entered = threading.Event(), threading.Event()

    def callback(access, refresh):
        entered.set()
        gate.wait(5)

    chat = refreshing(http, clock, callback, rounds=1)
    chat._stop_timeout = 0.05
    chat.is_available()
    chat._ensure_auth(force=True)
    assert entered.wait(3)
    dispatcher = chat._dispatcher               # still running; nothing is waiting
    assert chat._cb_pending is None
    with caplog.at_level(logging.WARNING):
        chat.stop()
    assert chat.stopped_cleanly is False and 'incomplete' in caplog.text
    gate.set()
    dispatcher.join(3)
    assert not dispatcher.is_alive()


def test_stop_reports_a_pair_that_is_still_waiting(http, clock):
    chat = make(http, clock, on_token_refresh=lambda a, r: None)
    chat._cb_pending = ('x', 'y')               # nobody is draining it
    chat.stop()
    assert chat.stopped_cleanly is False


def test_stop_drains_and_ends_the_dispatcher(http, clock):
    seen = []
    chat = refreshing(http, clock, lambda a, r: seen.append(a), rounds=1)
    chat.is_available()
    chat._ensure_auth(force=True)
    dispatcher = chat._dispatcher
    chat.stop()
    assert seen == ['A1'] and chat.stopped_cleanly is True and not dispatcher.is_alive()


# ----- C. recovery cooldown -----------------------------------------------------

def count_resets(monkeypatch):
    resets = []
    monkeypatch.setattr(tw, 'reset_secret_cache', lambda: resets.append(1))
    return resets


def test_failed_recovery_attempt_keeps_its_cooldown(http, clock, monkeypatch):
    resets = count_resets(monkeypatch)
    http.on('get', '/oauth2/validate', Resp(401), Resp(500))
    http.on('post', '/oauth2/token', Resp(400, {}))
    chat = make(http, clock)
    assert chat.is_available() is False
    clock.now += tw.AUTH_COOLDOWN + 1
    for _ in range(5):
        assert chat._ensure_auth() is False
    assert len(resets) == 1
    assert len(validate_calls(http)) == 2
    clock.now += tw.AUTH_COOLDOWN + 1
    chat._ensure_auth()
    assert len(resets) == 2


def test_missing_credentials_get_cooldown_guarded_cache_recovery(http, clock, creds, monkeypatch):
    resets = count_resets(monkeypatch)
    creds['access_token'] = ''
    chat = make(http, clock)
    assert chat.is_available() is False
    for _ in range(4):
        chat.is_available()
    assert resets == []
    clock.now += tw.AUTH_COOLDOWN + 1
    assert chat.is_available() is False   # still missing: one reset, no storm
    for _ in range(4):
        chat.is_available()
    assert len(resets) == 1
    creds['access_token'] = ACCESS         # the corrected value appears after the reset
    clock.now += tw.AUTH_COOLDOWN + 1
    assert chat.is_available() is True
    assert len(resets) == 2


# ----- D. replay protection -------------------------------------------------------

def test_replay_inside_the_window_after_many_distinct_ids_is_refused(http, clock):
    chat, got = primed(http, clock)
    chat._dispatch(notification(chat_event(), 'A'))
    for i in range(4096 + 50):
        chat._dispatch(notification(chat_event(message_id=f'x{i}'), f'filler-{i}'))
    delivered = len(got)
    chat._dispatch(notification(chat_event(), 'A'))  # fresh timestamp, same id
    assert len(got) == delivered


@pytest.mark.parametrize('value', ['2099-01-01T00:00:00Z', None])
def test_far_future_timestamp_is_rejected(http, clock, value):
    chat, got = primed(http, clock)
    ts = value if value else iso(tw.FUTURE_SKEW + 30)
    chat._dispatch(notification(chat_event(), 'f', ts=ts))
    assert got == []
    chat._dispatch(notification(chat_event(message_id='ok'), 'g', ts=iso(tw.FUTURE_SKEW - 30)))
    assert [e.message_id for e in got] == ['ok']


@pytest.mark.parametrize('value', [
    '2024-01-01T00:00:00+99:99', '2024-01-01T00:00:00+24:00',
    '2024-01-01T00:00:00+05:60', '2024-01-01T00:00:00-00:99', '2024-01-01T00:00:60Z',
    '2024-01-01T00:00:00+0100', '2024-01-01 00:00:00Z',
])
def test_malformed_timestamp_offsets_are_rejected(value):
    assert tw._parse_timestamp(value) is None


def test_valid_offsets_are_applied():
    assert tw._parse_timestamp('2024-01-01T00:00:00+01:00') == \
        tw._parse_timestamp('2023-12-31T23:00:00Z')
    assert tw._parse_timestamp('2024-01-01T00:00:00-05:30') == \
        tw._parse_timestamp('2024-01-01T05:30:00.000000000Z')


def test_id_cache_retains_for_the_whole_window_then_expires(http, clock):
    chat, got = primed(http, clock)
    chat._dispatch(notification(chat_event(), 'first'))
    clock.now += tw.SEEN_RETENTION - 1
    chat._dispatch(notification(chat_event(), 'first'))
    assert len(got) == 1                       # still remembered at the edge
    clock.now += 2
    chat._dispatch(notification(chat_event(message_id='late'), 'late'))
    assert list(chat._seen) == ['late']        # expired by time, nothing else kept


def test_hard_cap_drops_the_oldest_and_says_so_once(http, clock, monkeypatch, caplog):
    monkeypatch.setattr(tw, 'SEEN_IDS_CAP', 5)
    chat, got = primed(http, clock)
    with caplog.at_level(logging.WARNING):
        for i in range(9):
            chat._dispatch(notification(chat_event(message_id=f'm{i}'), f'id-{i}'))
    assert len(chat._seen) == 5 and 'id-0' not in chat._seen and 'id-8' in chat._seen
    warnings = [r for r in caplog.records if 'replay' in r.getMessage()]
    assert len(warnings) == 1


# ----- E. the bounded reader must exist, or the adapter is unavailable -------------

def test_probe_accepts_the_bounded_reader_and_rejects_the_stock_one():
    assert tw._probe_reader(tw._BoundedWebSocket) is None
    assert tw._probe_reader(websocket.WebSocket) is not None


def test_missing_frame_hook_is_reported_not_papered_over(monkeypatch):
    monkeypatch.delattr(frame_buffer, 'recv_length')
    cls, error = tw._make_bounded_websocket()
    assert cls is None and 'recv_length' in error


def test_renamed_internals_fail_the_probe(monkeypatch):
    # The hook still exists, but no longer does what the subclass relies on.
    monkeypatch.setattr(frame_buffer, 'recv_length', lambda self: None)
    cls, error = tw._make_bounded_websocket()
    assert cls is not None and error is None
    assert tw._probe_reader(cls) is not None


def stock_connection_recorder(monkeypatch):
    calls = []
    monkeypatch.setattr(tw.websocket, 'create_connection',
                        lambda url, **kw: calls.append(url) or pytest.fail('stock reader used'))
    return calls


@pytest.mark.parametrize('how', ['missing', 'probe-fails'])
def test_unavailable_with_one_clear_error_never_the_stock_reader(
        http, clock, monkeypatch, caplog, how):
    calls = stock_connection_recorder(monkeypatch)
    if how == 'missing':
        monkeypatch.setattr(tw, '_BoundedWebSocket', None)
        monkeypatch.setattr(tw, '_BOUNDED_ERROR', 'frame hook recv_length is missing')
    else:
        monkeypatch.setattr(tw, '_BoundedWebSocket', websocket.WebSocket)
    chat = TwitchChat(http=http, clock=clock, backoff_base=0.001, long_rest=0.01, auth_poll=0.01)
    with caplog.at_level(logging.DEBUG):
        assert chat.is_available() is False
        assert chat.is_available() is False
        chat.start(lambda e: None)
        time.sleep(0.2)
        chat.stop()
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and websocket.__version__ in errors[0].getMessage()
    with pytest.raises(OSError):
        tw._default_connect('wss://eventsub.wss.twitch.tv/ws', 1)
    assert calls == [] and http.posts('/eventsub/subscriptions') == []


# ----- F. expires_in ------------------------------------------------------------------

@pytest.mark.parametrize('value', [0, -5])
def test_expired_lifetime_refreshes_now(http, clock, value):
    http.on('get', '/oauth2/validate', valid_body(expires_in=value), valid_body())
    http.on('post', '/oauth2/token', Resp(200, {'access_token': NEW_ACCESS,
                                                 'refresh_token': NEW_REFRESH}))
    chat = make(http, clock)
    assert chat.is_available() is True
    assert len(http.posts('/oauth2/token')) == 1


@pytest.mark.parametrize('value', [0, -5])
def test_expired_lifetime_with_failed_refresh_is_refused(http, clock, value):
    http.on('get', '/oauth2/validate', valid_body(expires_in=value))
    http.on('post', '/oauth2/token', Resp(400, {}))
    chat = make(http, clock)
    assert chat.is_available() is False
    assert chat._snapshot is None


def test_replacement_that_validates_as_expired_is_refused(http, clock):
    http.on('get', '/oauth2/validate', Resp(401), valid_body(expires_in=0))
    http.on('post', '/oauth2/token', Resp(200, {'access_token': NEW_ACCESS,
                                                 'refresh_token': NEW_REFRESH}))
    assert make(http, clock).is_available() is False


@pytest.mark.parametrize('value', ['abc', None, True, [], {}, 'NaN'])
def test_malformed_lifetime_is_unknown_not_expired(http, clock, value):
    http.on('get', '/oauth2/validate', valid_body(expires_in=value))
    chat = make(http, clock)
    assert chat.is_available() is True
    assert http.posts('/oauth2/token') == []
    assert chat._snapshot.expires_at is None


def test_absent_lifetime_is_unknown_not_expired(http, clock):
    body = valid_body().json()
    del body['expires_in']
    http.on('get', '/oauth2/validate', Resp(200, body))
    chat = make(http, clock)
    assert chat.is_available() is True
    assert http.posts('/oauth2/token') == []
    assert chat._snapshot.expires_at is None


def test_deadline_is_anchored_before_the_validation_request(http, clock):
    class Slow(Resp):
        def json(self):
            clock.now += 500  # the validation took a long time
            return self._body

    http.on('get', '/oauth2/validate', Slow(200, valid_body(expires_in=1000).json()))
    chat = make(http, clock)
    start = clock.now
    assert chat.is_available() is True
    assert chat._snapshot.expires_at == start + 1000


def test_near_expiry_with_failed_refresh_stays_usable_until_the_deadline(http, clock):
    http.on('get', '/oauth2/validate', valid_body(expires_in=600), Resp(500))
    http.on('post', '/oauth2/token', Resp(400, {}))
    chat = make(http, clock)
    assert chat.is_available() is True
    clock.now += 300
    assert chat.is_available() is True
    clock.now += 301
    assert chat.is_available() is False


# ----- G. connection ownership -----------------------------------------------------

class StubConn:
    def __init__(self, status):
        self.status, self.closed = status, False

    def getstatus(self):
        if isinstance(self.status, Exception):
            raise self.status
        return self.status

    def close(self):
        self.closed = True


@pytest.mark.parametrize('status', [RuntimeError('SYNTHETIC-SECRET'), 302])
def test_connection_is_closed_when_the_handshake_check_fails(monkeypatch, status):
    stub = StubConn(status)
    monkeypatch.setattr(tw.websocket, 'create_connection', lambda url, **kw: stub)
    with pytest.raises(Exception):
        tw._default_connect('wss://eventsub.wss.twitch.tv/ws', 1)
    assert stub.closed


def test_a_good_connection_is_handed_over_open(monkeypatch):
    stub = StubConn(101)
    monkeypatch.setattr(tw.websocket, 'create_connection', lambda url, **kw: stub)
    assert tw._default_connect('wss://eventsub.wss.twitch.tv/ws', 1) is stub
    assert not stub.closed


# ----- H. response-derived text is never logged verbatim ------------------------------

INJECTION = 'SYNTHETIC-SECRET\nforged entry'


def test_revocation_status_is_allowlisted(http, clock, caplog):
    chat, got = primed(http, clock)
    frame = {'metadata': {'message_type': 'revocation'},
             'payload': {'subscription': {'status': INJECTION}}}
    with caplog.at_level(logging.DEBUG):
        assert chat._dispatch(frame) == ''
    assert 'SYNTHETIC-SECRET' not in caplog.text and 'forged' not in caplog.text
    assert chat._auth_failed_at is None  # only the documented value fails auth


def test_documented_revocation_status_is_logged(http, clock, caplog):
    chat, got = primed(http, clock)
    frame = {'metadata': {'message_type': 'revocation'},
             'payload': {'subscription': {'status': 'user_removed'}}}
    with caplog.at_level(logging.DEBUG):
        chat._dispatch(frame)
    assert 'user_removed' in caplog.text


@pytest.mark.parametrize('code', [INJECTION, 12345, None, {'a': 1}])
def test_drop_reason_code_is_allowlisted(http, clock, caplog, code):
    http.on('post', '/chat/messages', Resp(200, {'data': [
        {'is_sent': False, 'drop_reason': {'code': code, 'message': INJECTION}}]}))
    chat = make(http, clock)
    with caplog.at_level(logging.DEBUG):
        assert chat.send('111', 'hi') is False
    assert 'SYNTHETIC-SECRET' not in caplog.text and 'forged' not in caplog.text
    assert len(http.posts('/chat/messages')) == 1
