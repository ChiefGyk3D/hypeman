# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Discord chat: the gateway protocol, event conversion and REST sending, all
against a fake websocket and a fake HTTP session. Ids are synthetic snowflakes.
"""

import json
import logging
import time

import pytest
import websocket

from hypeman_social.social import CHAT_REGISTRY
from hypeman_social.social import discord_chat as dc
from hypeman_social.social.discord_chat import DiscordChat
from hypeman_social.social.events import ROLE_BROADCASTER, ROLE_MODERATOR

FAKE_TOKEN = 'FAKE.tokenValue-9f8e7d6c5b4a'
BOT = '900000000000000001'
OWNER = '900000000000000002'
USER = '900000000000000003'
GUILD = '800000000000000001'
CHAN = '700000000000000001'
MOD_ROLE = '600000000000000001'
HOOK = '500000000000000001'


class Frame:
    def __init__(self, data):
        self.data = data


def text(op, d=None, s=None, t=None):
    return (1, Frame(json.dumps({'op': op, 'd': d, 's': s, 't': t})))


def close(code):
    return (8, Frame(code.to_bytes(2, 'big')))


class FakeWS:
    """Plays queued frames; a queued Exception is raised.

    An empty queue calls ``on_empty`` and times out when one is given, and
    otherwise drops the connection so a scripted session ends by itself.
    """

    def __init__(self, frames, on_empty=None):
        self.frames = list(frames)
        self.sent = []
        self.closed_with = []
        self.on_empty = on_empty

    def recv_data_frame(self, control_frame):
        if not self.frames:
            if not self.on_empty:
                raise websocket.WebSocketConnectionClosedException('script over')
            self.on_empty()
            raise websocket.WebSocketTimeoutException('t')
        item = self.frames.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def send(self, data):
        self.sent.append(json.loads(data))

    def close(self, status=1000):
        self.closed_with.append(status)

    def ops(self):
        return [m['op'] for m in self.sent]


@pytest.fixture
def chat(monkeypatch):
    monkeypatch.setenv('DISCORD_BOT_TOKEN', FAKE_TOKEN)
    monkeypatch.setenv('DISCORD_MODERATOR_ROLE_IDS', MOD_ROLE)
    monkeypatch.delenv('DISCORD_GUILD_ID', raising=False)
    monkeypatch.delenv('DISCORD_CHANNEL_IDS', raising=False)
    monkeypatch.delenv('DOPPLER_TOKEN', raising=False)
    c = DiscordChat()
    c._bot_id = BOT
    c._owners[GUILD] = OWNER
    return c


def run_session(chat, frames, now=None):
    """Run one gateway connection against a scripted fake and return it."""
    ws = FakeWS(frames)
    chat._connect = lambda url: ws
    chat._wait = lambda seconds: False
    ws.url_used = None
    chat._stop_event.clear()
    chat._session()
    return ws


def msg(**over):
    base = {
        'id': '400000000000000001', 'channel_id': CHAN, 'guild_id': GUILD,
        'type': 0, 'content': 'hello', 'author': {'id': USER, 'username': 'someone'},
        'member': {'roles': []},
    }
    base.update(over)
    return base


# -- registry and config --------------------------------------------------

def test_registered():
    assert CHAT_REGISTRY['discord'] is DiscordChat
    assert DiscordChat().name == 'discord'


def test_available_needs_token_and_library(monkeypatch):
    monkeypatch.delenv('DISCORD_BOT_TOKEN', raising=False)
    monkeypatch.delenv('DOPPLER_TOKEN', raising=False)
    assert DiscordChat().is_available() is False
    monkeypatch.setenv('DISCORD_BOT_TOKEN', FAKE_TOKEN)
    assert DiscordChat().is_available() is True
    monkeypatch.setattr(dc, 'websocket', None)
    assert DiscordChat().is_available() is False


def test_availability_heals_when_token_appears(monkeypatch):
    monkeypatch.delenv('DISCORD_BOT_TOKEN', raising=False)
    monkeypatch.delenv('DOPPLER_TOKEN', raising=False)
    c = DiscordChat()
    assert c.is_available() is False
    monkeypatch.setenv('DISCORD_BOT_TOKEN', FAKE_TOKEN)
    assert c.is_available() is True


# -- protocol -------------------------------------------------------------

def test_hello_identifies_with_intents(chat):
    ws = run_session(chat, [text(10, {'heartbeat_interval': 40000})])
    identify = [m for m in ws.sent if m['op'] == 2][0]
    assert identify['d']['token'] == FAKE_TOKEN
    assert identify['d']['intents'] == 1 | 512 | 32768
    assert 6 not in ws.ops()


def test_ready_stores_session_and_bot_id(chat):
    chat._bot_id = None
    run_session(chat, [
        text(10, {'heartbeat_interval': 40000}),
        text(0, {'session_id': 'sess', 'resume_gateway_url': 'wss://r.example/',
                 'user': {'id': BOT}}, s=1, t='READY'),
    ])
    assert (chat._session_id, chat._bot_id, chat._seq) == ('sess', BOT, 1)
    assert chat._resume_url == 'wss://r.example/'


def test_heartbeat_sent_with_seq_and_zombie_detected(chat):
    clock = {'now': 100.0}
    chat._clock = lambda: clock['now']
    chat._jitter = lambda: 0.5
    chat._seq = None

    def tick():
        clock['now'] += 15.0  # 10 s interval: every empty poll passes a beat

    ws = FakeWS([text(10, {'heartbeat_interval': 10000}, s=7)], on_empty=tick)
    chat._connect = lambda url: ws
    chat._stop_event.clear()
    chat._session()
    beats = [m for m in ws.sent if m['op'] == 1]
    assert beats and beats[0]['d'] == 7
    # Never acked: the second beat sees the zombie and closes with a non-1000 code.
    assert ws.closed_with and ws.closed_with[0] == 4000


def test_ack_keeps_connection_alive(chat):
    clock = {'now': 0.0}
    chat._clock = lambda: clock['now']
    chat._jitter = lambda: 0.0
    state = {'n': 0}
    ws = FakeWS([text(10, {'heartbeat_interval': 1000})])

    def tick():
        state['n'] += 1
        clock['now'] += 0.6  # the ACK is read before the next beat falls due
        if state['n'] < 8:
            ws.frames.append(text(11))
        else:
            chat._stop_event.set()

    ws.on_empty = tick
    chat._connect = lambda url: ws
    chat._stop_event.clear()
    chat._session()
    assert len([m for m in ws.sent if m['op'] == 1]) >= 3
    assert 4000 not in ws.closed_with


def test_server_heartbeat_request_answered(chat):
    ws = run_session(chat, [text(10, {'heartbeat_interval': 40000}), text(1)])
    assert 1 in ws.ops()


def test_resume_after_reconnect_request(chat):
    chat._session_id, chat._resume_url, chat._seq = 'sess', 'wss://r.example/', 5
    urls = []
    ws = FakeWS([text(10, {'heartbeat_interval': 40000})])
    chat._connect = lambda url: urls.append(url) or ws
    chat._stop_event.clear()
    chat._session()
    assert urls[0].startswith('wss://r.example/') and 'v=10' in urls[0]
    resume = [m for m in ws.sent if m['op'] == 6][0]
    assert resume['d'] == {'token': FAKE_TOKEN, 'session_id': 'sess', 'seq': 5}
    assert 2 not in ws.ops()


def test_op7_ends_connection_but_keeps_session(chat):
    chat._session_id, chat._resume_url, chat._seq = 'sess', 'wss://r.example/', 5
    run_session(chat, [text(7)])
    assert chat._session_id == 'sess'


def test_invalid_session_true_keeps_session(chat):
    chat._session_id, chat._resume_url, chat._seq = 'sess', 'wss://r.example/', 5
    run_session(chat, [text(9, True)])
    assert chat._session_id == 'sess'


def test_invalid_session_false_drops_session_then_identifies(chat):
    chat._session_id, chat._resume_url, chat._seq = 'sess', 'wss://r.example/', 5
    waits = []
    ws = FakeWS([text(10, {'heartbeat_interval': 40000}), text(9, False)])
    chat._connect = lambda url: ws
    chat._wait = lambda s: waits.append(s) or False
    chat._stop_event.clear()
    chat._session()
    assert chat._session_id is None and chat._seq is None
    assert 1.0 <= waits[0] <= 5.0
    urls = []
    ws2 = FakeWS([text(10, {'heartbeat_interval': 40000})])
    chat._connect = lambda url: urls.append(url) or ws2
    chat._session()
    assert urls[0] == dc.GATEWAY_URL and 2 in ws2.ops()


@pytest.mark.parametrize('code', [4004, 4013, 4014])
def test_fatal_close_codes_stop_for_good(chat, code):
    run_session(chat, [close(code)])
    assert chat._fatal
    assert chat.is_available() is False


def test_intent_rejection_message_is_clear(chat):
    run_session(chat, [close(4014)])
    assert 'Message Content Intent' in chat._fatal
    assert FAKE_TOKEN not in chat._fatal


def test_session_lost_close_clears_session(chat):
    chat._session_id, chat._resume_url, chat._seq = 'sess', 'wss://r.example/', 5
    run_session(chat, [close(4009)])
    assert chat._session_id is None and not chat._fatal


def test_ordinary_close_resumes(chat):
    chat._session_id, chat._resume_url, chat._seq = 'sess', 'wss://r.example/', 5
    run_session(chat, [close(1006)])
    assert chat._session_id == 'sess' and not chat._fatal


def test_dropped_connection_returns_for_reconnect(chat):
    ws = run_session(chat, [websocket.WebSocketConnectionClosedException('gone')])
    assert ws.closed_with


def test_run_loop_backs_off_boundedly_and_resets_on_ready(chat):
    waits = []
    calls = {'n': 0}

    def session():
        calls['n'] += 1
        if calls['n'] == 5:
            chat._backoff = 1.0  # what READY does
        if calls['n'] >= 8:
            chat._stop_event.set()
        raise OSError('boom')

    chat._session = session
    chat._wait = lambda s: waits.append(s) or chat._stop_event.is_set()
    chat._run()
    assert waits[:4] == [1.0, 2.0, 4.0, 8.0]
    assert max(waits) <= dc.MAX_BACKOFF


def test_run_loop_stops_on_fatal(chat):
    def session():
        chat._fatal = 'nope'

    chat._session = session
    chat._run()
    assert chat._fatal == 'nope'


def test_start_stop_thread_lifecycle(chat):
    ws = FakeWS([text(10, {'heartbeat_interval': 40000})], on_empty=lambda: time.sleep(0.01))
    chat._connect = lambda url: ws
    seen = []
    chat.start(seen.append)
    time.sleep(0.2)
    assert chat._thread.is_alive()
    chat.stop()
    assert chat._thread is None
    assert not any(t.name == 'discord-chat' for t in __import__('threading').enumerate())


def test_handler_exception_does_not_kill_loop(chat):
    def boom(event):
        raise RuntimeError('handler bug')

    chat._handler = boom
    ws = run_session(chat, [
        text(10, {'heartbeat_interval': 40000}),
        text(0, msg(), s=2, t='MESSAGE_CREATE'),
        text(0, msg(id='400000000000000002'), s=3, t='MESSAGE_CREATE'),
    ])
    assert chat._seq == 3 and ws.frames == []


def test_message_create_reaches_handler(chat):
    got = []
    chat._handler = got.append
    run_session(chat, [text(10, {'heartbeat_interval': 40000}),
                       text(0, msg(), s=2, t='MESSAGE_CREATE')])
    assert [e.text for e in got] == ['hello']


def test_guild_create_records_owner(chat):
    chat._owners.clear()
    run_session(chat, [text(10, {'heartbeat_interval': 40000}),
                       text(0, {'id': GUILD, 'owner_id': OWNER}, s=2, t='GUILD_CREATE')])
    assert chat._owners[GUILD] == OWNER


def test_garbage_frame_is_ignored(chat):
    ws = run_session(chat, [(1, Frame('not json')), (1, Frame('[1]'))])
    assert ws.frames == []


# -- conversion -----------------------------------------------------------

def test_plain_message(chat):
    e = chat.to_event(msg(reply_to=None))
    assert (e.platform, e.channel, e.author_id, e.text) == ('discord', CHAN, USER, 'hello')
    assert e.message_id == '400000000000000001'
    assert not e.is_self and not e.bridged and e.roles == frozenset()


def test_author_id_not_name(chat):
    e = chat.to_event(msg(author={'id': USER, 'username': 'x', 'global_name': 'Pretty'}))
    assert e.author_id == USER and e.author_name == 'Pretty'


def test_webhook_message_is_bridged(chat):
    e = chat.to_event(msg(webhook_id=HOOK, author={'id': HOOK, 'username': 'Relayed', 'bot': True}))
    assert e.bridged and not e.is_self
    assert e.extra['webhook_id'] == HOOK


def test_other_bot_is_bridged(chat):
    e = chat.to_event(msg(author={'id': USER, 'username': 'ghost', 'bot': True}))
    assert e.bridged and e.extra['author_bot'] == 'true'
    assert 'webhook_id' not in e.extra


def test_self_is_flagged_and_not_bridged(chat):
    e = chat.to_event(msg(author={'id': BOT, 'username': 'me', 'bot': True}))
    assert e.is_self and not e.bridged


def test_dm_dropped(chat):
    m = msg()
    del m['guild_id']
    assert chat.to_event(m) is None


def test_system_and_empty_messages_dropped(chat):
    assert chat.to_event(msg(type=7)) is None
    assert chat.to_event(msg(content='')) is None
    assert chat.to_event(msg(author=None)) is None


def test_reply_reference(chat):
    e = chat.to_event(msg(type=19, message_reference={'message_id': '400000000000000009'}))
    assert e.reply_to_id == '400000000000000009'


def test_moderator_by_configured_role_only(chat):
    e = chat.to_event(msg(member={'roles': [MOD_ROLE]}))
    assert e.roles == frozenset({ROLE_MODERATOR})
    assert chat.to_event(msg(member={'roles': ['600000000000000099']})).roles == frozenset()


def test_owner_is_broadcaster(chat):
    e = chat.to_event(msg(author={'id': OWNER, 'username': 'owner'}))
    assert ROLE_BROADCASTER in e.roles


def test_owner_of_other_guild_is_nobody(chat):
    e = chat.to_event(msg(guild_id='800000000000000009', author={'id': OWNER, 'username': 'o'}))
    assert e.roles == frozenset()


def test_bridged_message_never_carries_roles(chat):
    e = chat.to_event(msg(webhook_id=HOOK, author={'id': OWNER, 'username': 'o', 'bot': True},
                          member={'roles': [MOD_ROLE]}))
    assert e.bridged and e.roles == frozenset()


def test_guild_and_channel_filters(chat, monkeypatch):
    monkeypatch.setenv('DISCORD_GUILD_ID', GUILD)
    monkeypatch.setenv('DISCORD_CHANNEL_IDS', f'{CHAN}, 700000000000000002,junk')
    chat._load_config()
    assert chat.to_event(msg()) is not None
    assert chat.to_event(msg(guild_id='800000000000000009')) is None
    assert chat.to_event(msg(channel_id='700000000000000009')) is None
    assert chat.to_event(msg(channel_id='700000000000000002')) is not None


# -- sending --------------------------------------------------------------

class Resp:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._p = payload
        self.headers = headers or {}

    def json(self):
        if self._p is None:
            raise ValueError('no body')
        return self._p


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_send_posts_with_safe_mentions_and_reply(chat):
    chat._http = FakeHTTP([Resp(200)])
    assert chat.send(CHAN, 'hi @everyone', reply_to_id='400000000000000001') is True
    url, kw = chat._http.calls[0]
    assert url.endswith(f'/channels/{CHAN}/messages')
    assert kw['headers']['Authorization'] == f'Bot {FAKE_TOKEN}'
    assert kw['json']['allowed_mentions'] == {'parse': []}
    assert kw['json']['message_reference']['message_id'] == '400000000000000001'
    assert kw['allow_redirects'] is False


def test_send_truncates_to_limit(chat):
    chat._http = FakeHTTP([Resp(200)])
    assert chat.send(CHAN, 'x' * 5000)
    assert len(chat._http.calls[0][1]['json']['content']) == dc.MESSAGE_LIMIT


def test_send_rejects_bad_input(chat):
    chat._http = FakeHTTP([])
    assert chat.send('../etc', 'hi') is False
    assert chat.send(CHAN, '') is False
    assert chat._http.calls == []


def test_send_without_token_is_false(chat):
    chat._token = None
    assert chat.send(CHAN, 'hi') is False


def test_429_waits_retry_after_then_succeeds(chat):
    slept = []
    chat._sleep = slept.append
    chat._http = FakeHTTP([Resp(429, {'retry_after': 1.5, 'global': False}), Resp(200)])
    assert chat.send(CHAN, 'hi') is True
    assert slept == [1.5]


def test_429_header_fallback(chat):
    slept = []
    chat._sleep = slept.append
    chat._http = FakeHTTP([Resp(429, None, {'Retry-After': '2'}), Resp(200)])
    assert chat.send(CHAN, 'hi') is True
    assert slept == [2.0]


def test_429_over_cap_gives_up_without_sleeping(chat):
    slept = []
    chat._sleep = slept.append
    chat._http = FakeHTTP([Resp(429, {'retry_after': 9999})])
    assert chat.send(CHAN, 'hi') is False
    assert slept == []


def test_429_forever_is_bounded(chat):
    chat._sleep = lambda s: None
    chat._http = FakeHTTP([Resp(429, {'retry_after': 1})] * 10)
    assert chat.send(CHAN, 'hi') is False
    assert len(chat._http.calls) <= dc.MAX_SEND_ATTEMPTS


def test_exhausted_bucket_delays_next_send(chat):
    slept = []
    chat._clock = lambda: 100.0
    chat._sleep = slept.append
    chat._http = FakeHTTP([
        Resp(200, None, {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset-After': '2.0'}),
        Resp(200)])
    assert chat.send(CHAN, 'a') and chat.send(CHAN, 'b')
    assert slept == [2.0]


def test_5xx_retried_then_succeeds(chat):
    chat._sleep = lambda s: None
    chat._http = FakeHTTP([Resp(502), Resp(200)])
    assert chat.send(CHAN, 'hi') is True


def test_4xx_not_retried(chat):
    chat._http = FakeHTTP([Resp(403)])
    assert chat.send(CHAN, 'hi') is False
    assert len(chat._http.calls) == 1


def test_send_never_raises(chat):
    chat._http = FakeHTTP([ConnectionError('network down')])
    assert chat.send(CHAN, 'hi') is False


# -- the token stays out of logs ------------------------------------------

def test_token_never_logged_or_raised(chat, caplog):
    caplog.set_level(logging.DEBUG)
    chat._http = FakeHTTP([ConnectionError(f'failed for Bot {FAKE_TOKEN}'), Resp(403), Resp(500)])
    chat._sleep = lambda s: None
    chat.send(CHAN, 'hi')
    chat.send(CHAN, 'hi')
    chat._session = lambda: (_ for _ in ()).throw(OSError(f'bad {FAKE_TOKEN}'))
    chat._wait = lambda s: True
    chat._run()
    del chat._session
    run_session(chat, [text(10, {'heartbeat_interval': 40000}), close(4004)])
    chat.is_available()
    assert FAKE_TOKEN not in caplog.text
    assert 'failed for Bot' in caplog.text or 'ConnectionError' in caplog.text
