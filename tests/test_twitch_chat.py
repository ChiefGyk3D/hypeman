# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
The Twitch chat adapter with the websocket and the HTTP layer faked. Synthetic
ids and obviously fake secrets only; nothing touches the network.
"""

import json
import logging
import queue
import threading

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

    def on(self, method, suffix, *responses):
        self.routes[(method, suffix)] = list(responses)

    def _do(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for (m, suffix), queue_ in self.routes.items():
            if m == method and url.endswith(suffix):
                item = queue_.pop(0) if len(queue_) > 1 else queue_[0]
                if isinstance(item, Exception):
                    raise item
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


def notification(event, mid='n-1'):
    return {'metadata': {'message_type': 'notification', 'message_id': mid,
                         'subscription_type': 'channel.chat.message'},
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
    chat.stop()
    assert not thread.is_alive()


def test_login_channel_resolved_via_helix(http, clock, creds):
    creds['channel'] = 'FakeChannel'
    http.on('get', '/users', Resp(200, {'data': [{'id': '111', 'login': 'fakechannel'}]}))
    chat = make(http, clock)
    assert chat.send('fakechannel', 'hi') is True
    assert http.posts('/chat/messages')[0][2]['json']['broadcaster_id'] == '111'
