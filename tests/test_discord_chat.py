# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Discord chat: the gateway protocol, event conversion and REST sending, all
against a fake websocket and a fake HTTP session. Ids are synthetic snowflakes.
"""

import copy
import json
import logging
import socket
import struct
import threading
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
RESUME = 'wss://gateway-us-east1-b.discord.gg/'
HELLO = {'heartbeat_interval': 40000}


class Frame:
    def __init__(self, data, fin=True):
        self.data = data
        self.fin = fin


def text(op, d=None, s=None, t=None):
    return (1, Frame(json.dumps({'op': op, 'd': d, 's': s, 't': t})))


def close(code):
    return (8, Frame(code.to_bytes(2, 'big')))


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
    """Plays queued POST responses; GET (the start-limit lookup) is a healthy
    answer unless queued."""

    def __init__(self, responses=(), gets=()):
        self.responses = list(responses)
        self.get_responses = list(gets)
        self.calls = []
        self.gets = []
        self.lock = threading.Lock()

    def post(self, url, **kw):
        if 'json' in kw:
            kw = dict(kw, json=copy.deepcopy(kw['json']))  # a snapshot: bodies are reused
        with self.lock:
            self.calls.append((url, kw))
            item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def get(self, url, **kw):
        with self.lock:
            self.gets.append((url, kw))
            item = self.get_responses.pop(0) if self.get_responses else start_limit()
        if isinstance(item, Exception):
            raise item
        return item


def start_limit(remaining=900, total=1000, reset_after=3600000, concurrency=1,
                url='wss://gateway.discord.gg/'):
    return Resp(200, {'url': url, 'shards': 1, 'session_start_limit': {
        'total': total, 'remaining': remaining, 'reset_after': reset_after,
        'max_concurrency': concurrency}})


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

    def getstatus(self):
        return 101

    def recv_data_frame(self, control_frame):
        if not self.frames:
            if not self.on_empty:
                raise websocket.WebSocketConnectionClosedException('script over')
            self.on_empty()
            raise websocket.WebSocketTimeoutException('t')
        item = self.frames.pop(0)
        if callable(item):
            item = item()
        if isinstance(item, Exception):
            raise item
        return item

    def send(self, data):
        self.sent.append(json.loads(data))

    def close(self, status=1000):
        self.closed_with.append(status)

    def ops(self):
        return [m['op'] for m in self.sent]


class BlockingWS(FakeWS):
    """Blocks in recv until released; abort() releases it unless told not to."""

    def __init__(self, frames=(), honours_abort=True):
        super().__init__(frames)
        self.release = threading.Event()
        self.entered = threading.Event()
        self.aborted = False
        self.honours_abort = honours_abort

    def recv_data_frame(self, control_frame):
        if self.frames:
            return super().recv_data_frame(control_frame)
        self.entered.set()
        self.release.wait(10)
        raise websocket.WebSocketConnectionClosedException('released')

    def abort(self):
        self.aborted = True
        if self.honours_abort:
            self.release.set()


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
    c._http = FakeHTTP()  # tests never touch the network
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
        text(0, {'session_id': 'sess', 'resume_gateway_url': RESUME,
                 'user': {'id': BOT}}, s=1, t='READY'),
    ])
    assert (chat._session_id, chat._bot_id, chat._seq) == ('sess', BOT, 1)
    assert chat._resume_url == RESUME


def test_heartbeat_sent_with_seq_and_zombie_detected(chat):
    clock = {'now': 100.0}
    chat._clock = lambda: clock['now']
    chat._jitter = lambda: 0.5
    chat._seq = None
    polls = {'n': 0}

    def tick():
        clock['now'] += 15.0  # 10 s interval: every empty poll passes a beat
        polls['n'] += 1
        if polls['n'] > 20:
            # Bounded: without zombie detection the loop would poll forever.
            chat._stop_event.set()

    ws = FakeWS([text(10, {'heartbeat_interval': 10000}, s=7)], on_empty=tick)
    chat._connect = lambda url: ws
    chat._stop_event.clear()
    chat._session()
    beats = [m for m in ws.sent if m['op'] == 1]
    assert beats and beats[0]['d'] == 7
    # Never acked: the second beat sees the zombie and closes with a non-1000 code.
    assert polls['n'] <= 20, 'zombie connection was never detected'
    assert ws.closed_with and ws.closed_with[0] == 4000
    assert len(beats) == 1


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
    chat._session_id, chat._resume_url, chat._seq = 'sess', RESUME, 5
    urls = []
    ws = FakeWS([text(10, {'heartbeat_interval': 40000})])
    chat._connect = lambda url: urls.append(url) or ws
    chat._stop_event.clear()
    chat._session()
    assert urls[0].startswith(RESUME) and 'v=10' in urls[0]
    resume = [m for m in ws.sent if m['op'] == 6][0]
    assert resume['d'] == {'token': FAKE_TOKEN, 'session_id': 'sess', 'seq': 5}
    assert 2 not in ws.ops()


def test_op7_ends_connection_but_keeps_session(chat):
    chat._session_id, chat._resume_url, chat._seq = 'sess', RESUME, 5
    ws = run_session(chat, [text(7)])
    assert chat._session_id == 'sess'
    # Discord invalidates the session on a 1000 or 1001 close: a resumable
    # reconnect must use another status.
    assert ws.closed_with and all(c not in (1000, 1001) for c in ws.closed_with)
    assert ws.closed_with[0] == dc.RESUMABLE_CLOSE_CODE


def test_invalid_session_true_keeps_session(chat):
    chat._session_id, chat._resume_url, chat._seq = 'sess', RESUME, 5
    ws = run_session(chat, [text(9, True)])
    assert chat._session_id == 'sess'
    assert all(c not in (1000, 1001) for c in ws.closed_with)


def test_shutdown_close_is_a_real_close(chat):
    """stop() wants the bot offline: the orderly 1000 close, not the resumable one."""
    chat._session_id, chat._resume_url, chat._seq = 'sess', RESUME, 5
    ws = FakeWS([text(10, HELLO)], on_empty=chat._stop_event.set)
    chat._connect = lambda url: ws
    chat._stop_event.clear()
    chat._session()
    assert ws.closed_with == [1000]


def test_session_lost_close_uses_orderly_status(chat):
    ws = run_session(chat, [text(10, HELLO), text(9, False)])
    assert chat._session_id is None and ws.closed_with == [1000]


def test_invalid_session_false_drops_session_then_identifies(chat):
    chat._session_id, chat._resume_url, chat._seq = 'sess', RESUME, 5
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


@pytest.mark.parametrize('code', [4004, 4010, 4011, 4012, 4013, 4014])
def test_fatal_close_codes_stop_for_good(chat, code):
    run_session(chat, [close(code)])
    assert chat._fatal == dc.FATAL_CLOSE_CODES[code]
    assert chat._fatal
    assert chat.is_available() is False


def test_intent_rejection_message_is_clear(chat):
    run_session(chat, [close(4014)])
    assert 'Message Content Intent' in chat._fatal
    assert FAKE_TOKEN not in chat._fatal


def test_session_lost_close_clears_session(chat):
    chat._session_id, chat._resume_url, chat._seq = 'sess', RESUME, 5
    run_session(chat, [close(4009)])
    assert chat._session_id is None and not chat._fatal


def test_ordinary_close_resumes(chat):
    chat._session_id, chat._resume_url, chat._seq = 'sess', RESUME, 5
    run_session(chat, [close(1006)])
    assert chat._session_id == 'sess' and not chat._fatal


def test_dropped_connection_returns_for_reconnect(chat):
    ws = run_session(chat, [websocket.WebSocketConnectionClosedException('gone')])
    assert ws.closed_with


def test_run_loop_backs_off_boundedly(chat):
    waits = []
    calls = {'n': 0}

    def session():
        calls['n'] += 1
        if calls['n'] >= 12:
            chat._stop_event.set()
        raise OSError('boom')

    chat._session = session
    chat._wait = lambda s: waits.append(s) or chat._stop_event.is_set()
    chat._run()
    assert waits[:4] == [1.0, 2.0, 4.0, 8.0]
    assert max(waits) <= dc.MAX_BACKOFF
    assert waits[-1] == dc.MAX_BACKOFF


def scripted_run(chat, behaviours, clock, limit=10_000.0):
    """Drive _run on a virtual clock. Each behaviour is an Exception or a FakeWS."""
    waits = []
    pending = list(behaviours)
    chat._clock = lambda: clock['now']
    chat._jitter = lambda: 0.0

    def connect(url):
        item = pending.pop(0) if pending else OSError('script over')
        if isinstance(item, Exception):
            raise item
        return item

    def wait(seconds):
        waits.append(seconds)
        clock['now'] += seconds
        if not pending or clock['now'] >= limit:
            chat._stop_event.set()
        return chat._stop_event.is_set()

    chat._connect = connect
    chat._wait = wait
    chat._stop_event.clear()
    chat._run()
    return waits


READY_BODY = {'session_id': 'sess', 'resume_gateway_url': RESUME, 'user': {'id': BOT}}


def ready_ws(lifetime, clock):
    """A session that is READY, then lives ``lifetime`` virtual seconds, then drops."""
    ws = FakeWS([text(10, HELLO), text(0, READY_BODY, s=1, t='READY')])
    state = {'t': 0.0}

    def tick():
        clock['now'] += 10.0
        state['t'] += 10.0
        ws.frames.append(close(1006) if state['t'] >= lifetime else text(11))

    ws.on_empty = tick
    return ws


def test_ready_alone_does_not_reset_backoff(chat):
    """A connection that dies right after READY has not earned a fresh backoff."""
    clock = {'now': 0.0}
    quick = FakeWS([text(10, HELLO), text(0, READY_BODY, s=1, t='READY'), close(1006)])
    waits = scripted_run(chat, [OSError('a'), OSError('b'), quick, OSError('c')], clock)
    assert waits[:3] == [1.0, 2.0, 4.0]


def test_backoff_resets_after_a_healthy_connection(chat):
    clock = {'now': 0.0}
    healthy = ready_ws(dc.STABLE_SECONDS + 10, clock)
    waits = scripted_run(chat, [OSError('a'), OSError('b'), healthy, OSError('c')], clock)
    assert waits[:3] == [1.0, 2.0, 1.0]


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
    monkeypatch.setenv('DISCORD_CHANNEL_IDS', f'{CHAN}, 700000000000000002,')
    chat._load_config()
    assert chat.to_event(msg()) is not None
    assert chat.to_event(msg(guild_id='800000000000000009')) is None
    assert chat.to_event(msg(channel_id='700000000000000009')) is None
    assert chat.to_event(msg(channel_id='700000000000000002')) is not None


def test_absent_or_blank_filters_are_unrestricted_by_design(chat, monkeypatch):
    monkeypatch.setenv('DISCORD_GUILD_ID', '  ')
    monkeypatch.setenv('DISCORD_CHANNEL_IDS', '')
    assert chat.is_available() is True
    assert chat.to_event(msg(guild_id='800000000000000009')) is not None


@pytest.mark.parametrize('setting,value', [
    ('DISCORD_GUILD_ID', 'oops'),
    ('DISCORD_GUILD_ID', '123abc'),
    ('DISCORD_GUILD_ID', '\u0661\u0662\u0663'),  # Arabic-Indic digits: isdigit() but not ASCII
    ('DISCORD_CHANNEL_IDS', 'oops'),
    ('DISCORD_CHANNEL_IDS', f'{CHAN}, oops'),
    ('DISCORD_CHANNEL_IDS', ','),
    ('DISCORD_CHANNEL_IDS', '\u0661\u0662'),
])
def test_invalid_filter_fails_closed(chat, monkeypatch, caplog, setting, value):
    """A supplied but invalid filter must not silently widen access."""
    monkeypatch.setenv(setting, value)
    caplog.set_level(logging.DEBUG)
    assert chat.is_available() is False
    assert setting in caplog.text
    assert value not in caplog.text
    assert chat.to_event(msg(guild_id='800000000000000009')) is None
    assert chat.to_event(msg()) is None
    chat.start(lambda e: None)
    assert chat._thread is None


def test_invalid_filter_error_is_logged_once_per_check(chat, monkeypatch, caplog):
    monkeypatch.setenv('DISCORD_GUILD_ID', 'oops')
    caplog.set_level(logging.WARNING)
    chat.is_available()
    assert sum('DISCORD_GUILD_ID' in r.getMessage() for r in caplog.records) == 1


def test_fixing_the_filter_heals(chat, monkeypatch):
    monkeypatch.setenv('DISCORD_GUILD_ID', 'oops')
    assert chat.is_available() is False
    monkeypatch.setenv('DISCORD_GUILD_ID', GUILD)
    assert chat.is_available() is True
    assert chat.to_event(msg(guild_id='800000000000000009')) is None
    assert chat.to_event(msg()) is not None


def test_snowflakes_are_ascii_digits_only(chat, monkeypatch):
    monkeypatch.setenv('DISCORD_MODERATOR_ROLE_IDS', f'{MOD_ROLE}, \u0661\u0662\u0663')
    chat._load_config()
    assert chat._cfg.mod_role_ids == frozenset({MOD_ROLE})
    chat._http = FakeHTTP([Resp(200)])
    assert chat.send('\u0661\u0662\u0663', 'hi') is False
    assert chat._http.calls == []
    assert chat.send(CHAN, 'hi', reply_to_id='\u0661\u0662') is True
    assert 'message_reference' not in chat._http.calls[0][1]['json']


# -- sending --------------------------------------------------------------

def ticking(chat, now=100.0):
    """A virtual clock that only the (fake) sleep advances, so waits really elapse."""
    clock = {'now': now}
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        clock['now'] += seconds

    chat._clock = lambda: clock['now']
    chat._sleep = sleep
    return slept, clock


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


def test_send_without_token_is_false(chat, monkeypatch):
    monkeypatch.delenv('DISCORD_BOT_TOKEN')
    chat._load_config()
    assert chat.send(CHAN, 'hi') is False


def test_429_waits_retry_after_then_succeeds(chat):
    slept, _ = ticking(chat)
    chat._http = FakeHTTP([Resp(429, {'retry_after': 1.5, 'global': False}), Resp(200)])
    assert chat.send(CHAN, 'hi') is True
    assert slept == [1.5]


def test_429_header_fallback(chat):
    slept, _ = ticking(chat)
    chat._http = FakeHTTP([Resp(429, None, {'Retry-After': '2'}), Resp(200)])
    assert chat.send(CHAN, 'hi') is True
    assert slept == [2.0]


def test_429_over_cap_gives_up_without_sleeping(chat):
    slept, _ = ticking(chat)
    chat._http = FakeHTTP([Resp(429, {'retry_after': 9999})])
    assert chat.send(CHAN, 'hi') is False
    assert slept == []


def test_429_forever_is_bounded(chat):
    ticking(chat)
    chat._http = FakeHTTP([Resp(429, {'retry_after': 1})] * 10)
    assert chat.send(CHAN, 'hi') is False
    assert len(chat._http.calls) <= dc.MAX_SEND_ATTEMPTS


def test_exhausted_bucket_delays_next_send(chat):
    slept, _ = ticking(chat)
    chat._http = FakeHTTP([
        Resp(200, None, {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset-After': '2.0'}),
        Resp(200)])
    assert chat.send(CHAN, 'a') and chat.send(CHAN, 'b')
    assert slept == [2.0]


def test_5xx_retry_is_deduplicated_by_an_enforced_nonce(chat):
    """502 then 200 means two POSTs, so both must carry the same enforced nonce:
    Discord then returns the first message instead of creating a second."""
    chat._sleep = lambda s: None
    chat._http = FakeHTTP([Resp(502), Resp(200)])
    assert chat.send(CHAN, 'hi') is True
    first, second = (kw['json'] for _, kw in chat._http.calls)
    assert first['nonce'] == second['nonce']
    assert first['enforce_nonce'] is True and second['enforce_nonce'] is True
    assert 0 < len(str(first['nonce'])) <= 25


def test_distinct_sends_get_distinct_nonces(chat):
    chat._http = FakeHTTP([Resp(200), Resp(200)])
    chat.send(CHAN, 'a')
    chat.send(CHAN, 'a')
    nonces = {kw['json']['nonce'] for _, kw in chat._http.calls}
    assert len(nonces) == 2


def test_ambiguous_transport_failure_is_not_replayed(chat):
    chat._sleep = lambda s: None
    chat._http = FakeHTTP([TimeoutError('read timed out'), Resp(200)])
    assert chat.send(CHAN, 'hi') is False
    assert len(chat._http.calls) == 1


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


# -- the token only ever goes to Discord (resume URLs, redirects) ---------

@pytest.mark.parametrize('url', [
    'wss://gateway.discord.gg/',
    'wss://gateway.discord.gg/?v=10&encoding=json',
    'wss://gateway.discord.gg:443/',
    'wss://gateway-us-east1-b.discord.gg/',
    'WSS://Gateway.Discord.GG/',
])
def test_trusted_gateway_urls(url):
    assert dc.is_trusted_gateway_url(url) is True


@pytest.mark.parametrize('url', [
    'ws://attacker.invalid/',
    'ws://gateway.discord.gg/',
    'https://gateway.discord.gg/',
    'wss://user@evil.invalid:8443/',
    'wss://gateway.discord.gg@evil.invalid/',
    'wss://gateway.discord.gg:pw@evil.invalid/',
    'wss://user@gateway.discord.gg/',
    'wss://user:pw@gateway-us-east1-b.discord.gg/',
    'wss://gateway.discord.gg\\@evil.invalid/',
    'wss://gateway.discord.gg.evil.invalid/',
    'wss://evildiscord.gg/',
    'wss://discord.gg.evil.invalid/',
    'wss://discord.gg/',
    'wss://gateway.discord.gg./',
    'wss://gateway.discord.gg:8443/',
    'wss://gateway.discord.gg:80/',
    'wss://gateway.discord.gg:notaport/',
    'wss://gateway.discord.gg/#frag',
    'wss://gateway.discord.gg/ x',
    'wss://gateway.discord.gg/\\n',
    'wss://gateway%2ediscord.gg.evil.invalid/',
    'wss:///path',
    'wss://',
    '',
    None,
    42,
])
def test_untrusted_gateway_urls(url):
    assert dc.is_trusted_gateway_url(url) is False


@pytest.mark.parametrize('bad', [
    'ws://attacker.invalid/',
    'wss://user@evil.invalid:8443/',
    'wss://gateway.discord.gg.evil.invalid/',
])
def test_untrusted_resume_url_is_never_connected_and_never_sent_the_token(chat, bad):
    chat._session_id, chat._resume_url, chat._seq = 'sess', bad, 5
    urls = []
    ws = FakeWS([text(10, HELLO)])
    chat._connect = lambda url: urls.append(url) or ws
    chat._wait = lambda s: False
    chat._stop_event.clear()
    chat._session()
    assert urls == [dc.GATEWAY_URL]
    assert 6 not in ws.ops() and 2 in ws.ops()
    assert chat._session_id is None


def test_ready_with_untrusted_resume_url_is_not_remembered(chat):
    run_session(chat, [
        text(10, HELLO),
        text(0, dict(READY_BODY, resume_gateway_url='wss://evil.invalid/'), s=1, t='READY'),
    ])
    assert chat._resume_url is None


def test_an_untrusted_lookup_url_is_never_connected(chat):
    chat._http = FakeHTTP(gets=[start_limit(url='wss://evil.invalid/')])
    urls = []
    chat._connect = lambda url: urls.append(url) or FakeWS([])
    chat._stop_event.clear()
    chat._session()
    assert urls == [dc.GATEWAY_URL]


def test_start_limit_lookup_ignores_an_untrusted_url(chat):
    chat._http = FakeHTTP(gets=[start_limit(url='wss://evil.invalid/')])
    assert chat._start_limit(FAKE_TOKEN)[0] == dc.GATEWAY_URL
    chat._http = FakeHTTP(gets=[start_limit(url='wss://gateway-us-east1-b.discord.gg/')])
    assert chat._start_limit(FAKE_TOKEN)[0].startswith('wss://gateway-us-east1-b.discord.gg/?v=10')


def test_connect_disables_handshake_redirects(chat, monkeypatch):
    seen = {}

    def fake_create(url, **kw):
        seen.update(kw)
        return FakeWS([])

    monkeypatch.setattr(dc.websocket, 'create_connection', fake_create)
    chat._connect(dc.GATEWAY_URL)
    assert seen.get('redirect_limit') == 0


def test_a_real_redirect_is_not_followed():
    """A server that answers the handshake with a 302 must not get the client to
    connect anywhere else (the Resume that follows carries the token)."""
    decoy = socket.socket()
    decoy.bind(('127.0.0.1', 0))
    decoy.listen(1)
    decoy.settimeout(0.5)
    decoy_port = decoy.getsockname()[1]
    origin = socket.socket()
    origin.bind(('127.0.0.1', 0))
    origin.listen(1)
    origin.settimeout(5)
    origin_port = origin.getsockname()[1]

    def serve():
        conn, _ = origin.accept()
        conn.recv(4096)
        conn.sendall(
            f'HTTP/1.1 302 Found\r\nLocation: ws://127.0.0.1:{decoy_port}/\r\n'
            'Content-Length: 0\r\n\r\n'.encode())
        conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        with pytest.raises(websocket.WebSocketException):
            DiscordChat()._connect(f'ws://127.0.0.1:{origin_port}/')
        t.join(5)
        with pytest.raises(socket.timeout):
            decoy.accept()  # nobody followed the redirect
    finally:
        decoy.close()
        origin.close()


# -- receive size limits --------------------------------------------------

def frame_header(length, opcode=1, fin=True):
    first = (0x80 if fin else 0) | opcode
    if length < 126:
        return bytes([first, length])
    if length < 65536:
        return bytes([first, 126]) + struct.pack('!H', length)
    return bytes([first, 127]) + struct.pack('!Q', length)


def real_ws(timeout=1.0, **options):
    """A real websocket-client object reading from a socketpair we control."""
    near, far = socket.socketpair()
    ws = dc._BoundedWebSocket(**options)
    ws.sock = near
    ws.connected = True
    ws.settimeout(timeout)
    return ws, far


def huge_payload():
    return b'{"op":0,"d":"' + '\U0001F600'.encode() * 1_000_000 + b'"}'


def test_oversize_frame_is_refused_without_parsing(chat, monkeypatch):
    payload = huge_payload()
    assert len(payload) > 4_000_000 > dc.MAX_MESSAGE_BYTES
    monkeypatch.setattr(dc.json, 'loads', lambda *a, **k: pytest.fail('oversize frame parsed'))
    got = []
    chat._handler = got.append
    ws = run_session(chat, [(1, Frame(payload))])
    assert ws.closed_with and ws.closed_with[0] == dc.OVERSIZE_CLOSE_CODE
    assert got == []
    assert chat._fatal and 'DISCORD_MAX_EVENT_BYTES' in chat._fatal


def test_size_is_counted_in_encoded_bytes_not_characters(chat, monkeypatch):
    # 600,000 characters, 2.4 million UTF-8 bytes: under a character limit, over the byte limit.
    body = '\U0001F600' * 600_000
    assert len(body) < dc.MAX_MESSAGE_BYTES < len(body.encode())
    monkeypatch.setattr(dc.json, 'loads', lambda *a, **k: pytest.fail('oversize frame parsed'))
    ws = run_session(chat, [(1, Frame(body))])
    assert ws.closed_with[0] == dc.OVERSIZE_CLOSE_CODE


def test_non_text_frames_are_ignored(chat):
    ws = run_session(chat, [(2, Frame(b'\x00\x01')), (9, Frame(b''))])
    assert ws.frames == [] and not chat._fatal


def test_deeply_nested_json_is_dropped_not_fatal(chat):
    nest = b'[' * 200_000 + b']' * 200_000
    assert len(nest) < dc.MAX_MESSAGE_BYTES
    ws = run_session(chat, [(1, Frame(nest))])
    assert ws.frames == []


def test_real_frame_header_over_the_limit_is_refused_before_the_payload_is_read():
    ws, far = real_ws()
    try:
        far.sendall(frame_header(4_000_034) + b'x' * 100)
        with pytest.raises(dc.MessageTooLarge):
            ws.recv_data_frame(True)
        # The payload was never consumed from the socket.
        far.setblocking(False)
        assert ws.sock.recv(1000, socket.MSG_PEEK) == b'x' * 100
    finally:
        ws.sock.close()
        far.close()


def test_real_fragments_are_reassembled_within_the_limit():
    ws, far = real_ws(max_bytes=100)
    try:
        far.sendall(frame_header(3, 1, fin=False) + b'abc' + frame_header(2, 0, fin=True) + b'de')
        opcode, frame = ws.recv_data_frame(True)
        assert (opcode, frame.data) == (1, b'abcde')
    finally:
        ws.sock.close()
        far.close()


def test_real_fragmented_message_over_the_cumulative_limit_is_refused_early():
    ws, far = real_ws(max_bytes=100)
    try:
        # Each fragment alone is fine; together they pass 100. Only the second
        # header is sent: a reader that waited for its payload would time out.
        far.sendall(frame_header(60, 1, fin=False) + b'a' * 60 + frame_header(60, 0, fin=True))
        with pytest.raises(dc.MessageTooLarge):
            ws.recv_data_frame(True)
    finally:
        ws.sock.close()
        far.close()


def test_connect_uses_the_bounded_websocket_with_the_configured_limit(chat, monkeypatch):
    monkeypatch.setenv('DISCORD_MAX_EVENT_BYTES', '12345')
    chat._load_config()
    chat._active = chat._cfg
    seen = {}
    monkeypatch.setattr(dc.websocket, 'create_connection',
                        lambda url, **kw: seen.update(kw) or FakeWS([]))
    chat._connect(dc.GATEWAY_URL)
    assert seen['class_'] is dc._BoundedWebSocket and seen['max_bytes'] == 12345


# -- stop(), start() and cancellation -------------------------------------

def worker_threads():
    return [t for t in threading.enumerate() if t.name == 'discord-chat' and t.is_alive()]


def test_stop_actively_closes_a_blocked_connection(chat, monkeypatch):
    monkeypatch.setattr(dc, 'STOP_JOIN_SECONDS', 0.2)
    ws = BlockingWS([text(10, HELLO)])
    chat._connect = lambda url: ws
    chat.start(lambda e: None)
    assert ws.entered.wait(5)
    assert chat.shutdown() is True
    assert ws.aborted and chat._thread is None and worker_threads() == []


def test_stop_reports_a_worker_that_will_not_exit_and_blocks_a_second_start(chat, monkeypatch):
    monkeypatch.setattr(dc, 'STOP_JOIN_SECONDS', 0.1)
    monkeypatch.setattr(dc, 'STOP_ABORT_SECONDS', 0.1)
    stuck = BlockingWS([text(10, HELLO)], honours_abort=False)
    chat._connect = lambda url: stuck
    chat.start(lambda e: None)
    assert stuck.entered.wait(5)
    worker = chat._thread
    first_event = chat._stop_event
    try:
        assert chat.shutdown() is False
        assert chat._thread is worker and worker.is_alive()
        assert first_event.is_set()
        chat.start(lambda e: None)
        assert worker_threads() == [worker]
        assert chat._stop_event is first_event and first_event.is_set()
    finally:
        stuck.release.set()
        worker.join(5)
    assert chat.shutdown() is True
    chat._connect = lambda url: FakeWS([text(10, HELLO)], on_empty=lambda: time.sleep(0.01))
    chat.start(lambda e: None)
    try:
        assert len(worker_threads()) == 1
        assert chat._stop_event is not first_event and not chat._stop_event.is_set()
    finally:
        chat.stop()


def test_concurrent_starts_make_one_worker(chat):
    chat._connect = lambda url: FakeWS([text(10, HELLO)], on_empty=lambda: time.sleep(0.01))
    barrier = threading.Barrier(8)

    def go():
        barrier.wait(5)
        chat.start(lambda e: None)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    try:
        assert len(worker_threads()) == 1
    finally:
        chat.stop()
    assert worker_threads() == []


def test_cancellation_while_receiving_hello_never_identifies(chat):
    def hello_after_cancel():
        chat._stop_event.set()
        return text(10, HELLO)

    ws = run_session(chat, [hello_after_cancel])
    assert ws.sent == []


def test_cancellation_while_receiving_hello_never_resumes(chat):
    chat._session_id, chat._resume_url, chat._seq = 'sess', RESUME, 5

    def hello_after_cancel():
        chat._stop_event.set()
        return text(10, HELLO)

    ws = run_session(chat, [hello_after_cancel])
    assert ws.sent == []


def test_cancellation_during_connect_sends_nothing(chat):
    ws = FakeWS([text(10, HELLO)])

    def connect(url):
        chat._stop_event.set()
        return ws

    chat._connect = connect
    chat._stop_event.clear()
    chat._session()
    assert ws.sent == [] and ws.closed_with


def test_cancelled_message_is_not_delivered(chat):
    got = []
    chat._handler = got.append

    def message_after_cancel():
        chat._stop_event.set()
        return text(0, msg(), s=2, t='MESSAGE_CREATE')

    run_session(chat, [text(10, HELLO), message_after_cancel])
    assert got == []


def test_each_start_gets_a_fresh_stop_event(chat):
    chat._connect = lambda url: FakeWS([text(10, HELLO)], on_empty=lambda: time.sleep(0.01))
    chat.start(lambda e: None)
    first = chat._stop_event
    assert chat.shutdown() is True
    chat.start(lambda e: None)
    try:
        assert chat._stop_event is not first and first.is_set()
    finally:
        chat.stop()


def test_fatal_close_then_new_token_and_start_recovers(chat, monkeypatch):
    monkeypatch.setattr(dc, 'IDENTIFY_SPACING_SECONDS', 0.0)
    chat._connect = lambda url: FakeWS([text(10, HELLO), close(4004)])
    chat.start(lambda e: None)
    chat._thread.join(5)
    assert chat._fatal and chat.is_available() is False

    monkeypatch.setenv('DISCORD_BOT_TOKEN', 'SYNTHETIC-NEW-bbbbbbbb')
    second = FakeWS([text(10, HELLO)], on_empty=lambda: time.sleep(0.01))
    chat._connect = lambda url: second
    chat.start(lambda e: None)
    try:
        assert chat._thread is not None and chat._thread.is_alive()
        deadline = time.time() + 5
        while not second.sent and time.time() < deadline:
            time.sleep(0.01)
        assert second.sent and second.sent[0]['d']['token'] == 'SYNTHETIC-NEW-bbbbbbbb'
        assert chat._fatal is None
    finally:
        chat.stop()


# -- Identify limits ------------------------------------------------------

def identify_cycle():
    """Hello, Identify, READY, then Discord loses the session."""
    return FakeWS([text(10, HELLO), text(0, READY_BODY, s=1, t='READY'), text(9, False)])


def cycling_run(chat, limit=1000.0, clock=None):
    clock = clock or {'now': 0.0}
    idents = []
    chat._clock = lambda: clock['now']
    chat._jitter = lambda: 0.0

    def connect(url):
        ws = identify_cycle()
        real_send = ws.send

        def send(data):
            real_send(data)
            if json.loads(data)['op'] == 2:
                idents.append(clock['now'])

        ws.send = send
        return ws

    def wait(seconds):
        clock['now'] += seconds
        if clock['now'] >= limit:
            chat._stop_event.set()
        return chat._stop_event.is_set()

    chat._connect = connect
    chat._wait = wait
    chat._stop_event.clear()
    chat._run()
    return idents


def test_identifies_stay_bounded_when_ready_keeps_being_followed_by_session_loss(chat):
    idents = cycling_run(chat, limit=1000.0)
    assert 2 <= len(idents) <= 12, len(idents)


def test_identifies_are_spaced_by_the_concurrency_limit(chat):
    idents = cycling_run(chat, limit=200.0)
    gaps = [b - a for a, b in zip(idents, idents[1:])]
    assert gaps and min(gaps) >= dc.IDENTIFY_SPACING_SECONDS


def test_spacing_is_five_seconds_whatever_max_concurrency_says(chat):
    """An unsharded client always lands in bucket 0, so max_concurrency is no licence."""
    chat._http = FakeHTTP(gets=[start_limit(concurrency=16)] * 50)
    idents = cycling_run(chat, limit=60.0)
    gaps = [b - a for a, b in zip(idents, idents[1:])]
    assert len(gaps) >= 3 and min(gaps) >= dc.IDENTIFY_SPACING_SECONDS


def test_start_limit_is_fetched_with_the_bot_token(chat):
    chat._http = FakeHTTP(gets=[start_limit(remaining=900)])
    cycling_run(chat, limit=1.0)
    url, kw = chat._http.gets[0]
    assert url.endswith('/gateway/bot')
    assert kw['headers']['Authorization'] == f'Bot {FAKE_TOKEN}'
    assert kw['allow_redirects'] is False


def test_low_remaining_budget_refuses_to_identify_and_reports_once(chat, caplog):
    chat._http = FakeHTTP(gets=[start_limit(remaining=dc.IDENTIFY_RESERVE)])
    caplog.set_level(logging.WARNING)
    idents = cycling_run(chat, limit=1000.0)
    assert idents == []
    assert chat._fatal and 'Identify' in chat._fatal
    assert chat.is_available() is False
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1


def test_local_count_is_an_extra_constraint(chat):
    clock = {'now': 100000.0}
    chat._clock = lambda: clock['now']
    chat._identify_times = [clock['now'] - 3600.0] * (dc.DAILY_IDENTIFY_LIMIT - dc.IDENTIFY_RESERVE)
    assert chat._identify_gate(FAKE_TOKEN) is None  # Discord said 900 remain; our count says no
    assert chat._fatal and 'Identify' in chat._fatal


def test_identifies_older_than_a_day_do_not_count(chat):
    clock = {'now': 200000.0}
    chat._clock = lambda: clock['now']
    chat._identify_times = [clock['now'] - 90000.0] * dc.DAILY_IDENTIFY_LIMIT
    assert chat._identify_gate(FAKE_TOKEN) == dc.GATEWAY_URL
    assert chat._identify_times == []


@pytest.mark.parametrize('answer', [
    Resp(503),
    Resp(200, {'url': 'wss://gateway.discord.gg/'}),
    Resp(200, {'session_start_limit': {'total': 1000, 'remaining': 900}}),
    Resp(200, {'session_start_limit': {'total': 1000, 'remaining': 'many', 'reset_after': 1,
                                       'max_concurrency': 1}}),
    Resp(200, {'session_start_limit': {'total': 1000, 'remaining': float('nan'), 'reset_after': 1,
                                       'max_concurrency': 1}}),
    Resp(200, {'session_start_limit': {'total': 1000, 'remaining': 900, 'reset_after': 1,
                                       'max_concurrency': 0}}),
    Resp(200, {'session_start_limit': {'total': 1000, 'remaining': 900, 'max_concurrency': 1}}),
    Resp(200, {'session_start_limit': {'total': 1000, 'remaining': 900, 'reset_after': 'soon',
                                       'max_concurrency': 1}}),
    Resp(200, {'session_start_limit': {'total': 1000, 'remaining': 900, 'reset_after': -1,
                                       'max_concurrency': 1}}),
    Resp(200, ['not', 'a', 'dict']),
    Resp(200, None),
    TimeoutError('slow'),
])
def test_no_valid_start_limit_means_no_fresh_identify(chat, answer):
    chat._http = FakeHTTP(gets=[answer])
    ws = run_session(chat, [text(10, HELLO)])
    assert ws.sent == [] and chat._identify_times == []
    assert not chat._fatal  # not stopped: the run loop retries with its backoff


def test_run_loop_retries_the_lookup_with_backoff_and_never_identifies_blind(chat):
    clock = {'now': 0.0}
    chat._http = FakeHTTP(gets=[Resp(503), Resp(503), TimeoutError('t')])
    behaviours = [FakeWS([text(10, HELLO)])]
    waits = scripted_run(chat, behaviours, clock)
    assert waits[:3] == [1.0, 2.0, 4.0]
    # Three failed lookups, each followed by a longer wait and no Identify; the
    # fourth answer was healthy.
    assert len(chat._http.gets) == 4 and chat._identify_times == [7.0]


def test_a_held_session_may_resume_without_a_lookup(chat):
    chat._http = FakeHTTP(gets=[Resp(503)])
    chat._session_id, chat._resume_url, chat._seq = 'sess', RESUME, 5
    ws = run_session(chat, [text(10, HELLO)])
    assert 6 in ws.ops() and chat._http.gets == []


def test_start_limit_failure_text_is_not_logged(chat, caplog):
    caplog.set_level(logging.DEBUG)
    chat._http = FakeHTTP(gets=[ConnectionError(f'down {FAKE_TOKEN}')])
    assert chat._start_limit(FAKE_TOKEN) is None
    assert FAKE_TOKEN not in caplog.text


# -- guild ownership ------------------------------------------------------

def feed(chat, kind, body, s=2):
    run_session(chat, [text(10, HELLO), text(0, body, s=s, t=kind)])


def role_of(chat, author):
    return chat.to_event(msg(author={'id': author, 'username': 'x'})).roles


def test_guild_update_moves_broadcaster_to_the_new_owner(chat):
    other = '900000000000000009'
    assert ROLE_BROADCASTER in role_of(chat, OWNER)
    chat._session_id, chat._resume_url, chat._seq = 'sess', RESUME, 1
    run_session(chat, [text(10, HELLO), text(0, {'id': GUILD, 'owner_id': other}, s=2,
                                              t='GUILD_UPDATE')])
    assert ROLE_BROADCASTER not in role_of(chat, OWNER)
    assert ROLE_BROADCASTER in role_of(chat, other)


def test_guild_update_without_owner_fails_closed(chat):
    chat._session_id, chat._resume_url, chat._seq = 'sess', RESUME, 1
    run_session(chat, [text(10, HELLO), text(0, {'id': GUILD}, s=2, t='GUILD_UPDATE')])
    assert role_of(chat, OWNER) == frozenset()


def test_guild_delete_forgets_the_owner(chat):
    chat._session_id, chat._resume_url, chat._seq = 'sess', RESUME, 1
    run_session(chat, [text(10, HELLO), text(0, {'id': GUILD}, s=2, t='GUILD_DELETE')])
    assert role_of(chat, OWNER) == frozenset()


def test_unavailable_guild_create_forgets_the_owner(chat):
    chat._session_id, chat._resume_url, chat._seq = 'sess', RESUME, 1
    run_session(chat, [text(10, HELLO), text(0, {'id': GUILD, 'unavailable': True,
                                                'owner_id': OWNER}, s=2, t='GUILD_CREATE')])
    assert role_of(chat, OWNER) == frozenset()


def test_unset_owner_never_grants_broadcaster(chat):
    chat._owners.clear()
    run_session(chat, [text(10, HELLO), text(0, {'id': GUILD}, s=2, t='GUILD_CREATE')])
    assert role_of(chat, OWNER) == frozenset()
    assert role_of(chat, 'None') == frozenset()


def test_fresh_session_rebuilds_ownership_from_scratch(chat):
    ghost = '700000000000000077'
    chat._owners[ghost] = OWNER  # stale: a guild the new session never reports
    run_session(chat, [text(10, HELLO)])
    assert chat._owners == {}


def test_ready_clears_ownership_cache(chat):
    run_session(chat, [text(10, HELLO), text(0, READY_BODY, s=1, t='READY'),
                       text(0, {'id': GUILD, 'owner_id': USER}, s=2, t='GUILD_CREATE')])
    assert chat._owners == {GUILD: USER}


def test_resumed_session_keeps_ownership(chat):
    chat._session_id, chat._resume_url, chat._seq = 'sess', RESUME, 1
    run_session(chat, [text(10, HELLO)])
    assert chat._owners == {GUILD: OWNER}


# -- credentials in logs --------------------------------------------------

def test_old_and_new_tokens_never_reach_the_logs(chat, monkeypatch, caplog):
    old, new = 'SYNTHETIC-OLD-aaaaaaaa', 'SYNTHETIC-NEW-bbbbbbbb'
    monkeypatch.setenv('DISCORD_BOT_TOKEN', old)
    chat._load_config()
    caplog.set_level(logging.DEBUG)

    class Reloading(FakeHTTP):
        def post(self, url, **kw):
            monkeypatch.setenv('DISCORD_BOT_TOKEN', new)
            chat.is_available()  # a credential reload lands mid-operation
            raise ConnectionError(f'rejected {old} then {new}')

    chat._http = Reloading()
    assert chat.send(CHAN, 'hi') is False
    chat._session = lambda: (_ for _ in ()).throw(OSError(f'bad {old} {new}'))
    chat._wait = lambda s: True
    chat._run()
    del chat._session
    chat._handler = lambda e: (_ for _ in ()).throw(RuntimeError(f'handler {old} {new}'))
    run_session(chat, [text(10, HELLO), text(0, msg(), s=2, t='MESSAGE_CREATE')])
    assert old not in caplog.text and new not in caplog.text
    assert 'ConnectionError' in caplog.text and 'OSError' in caplog.text


# -- rate limits across callers -------------------------------------------

def wait_for(predicate, seconds=2.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_global_429_holds_back_sends_to_other_channels(chat):
    clock = {'now': 100.0}
    chat._clock = lambda: clock['now']
    release = threading.Event()
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        release.wait(5)
        clock['now'] += seconds

    chat._sleep = sleep
    chat._http = FakeHTTP([Resp(429, {'retry_after': 2.0, 'global': True}), Resp(200), Resp(200)])
    a = threading.Thread(target=chat.send, args=(CHAN, 'a'))
    b = threading.Thread(target=chat.send, args=('700000000000000002', 'b'))
    a.start()
    assert wait_for(lambda: len(sleeps) == 1)
    b.start()
    try:
        # The second channel must wait out the global limit instead of posting.
        assert wait_for(lambda: len(sleeps) == 2)
        assert len(chat._http.calls) == 1
    finally:
        release.set()
        a.join(5)
        b.join(5)
    assert len(chat._http.calls) == 3


def test_a_deadline_extended_during_a_wait_is_honoured(chat):
    """A wakes at the deadline it computed; B has pushed it out meanwhile."""
    clock = {'now': 100.0}
    chat._clock = lambda: clock['now']
    chat._global_until = 101.0
    asleep, release = threading.Event(), threading.Event()
    sleeps = []
    posted_at = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            asleep.set()
            release.wait(5)
        clock['now'] += seconds

    class Timing(FakeHTTP):
        def post(self, url, **kw):
            posted_at.append(clock['now'])
            return Resp(200)

    chat._sleep = sleep
    chat._http = Timing()
    a = threading.Thread(target=chat.send, args=(CHAN, 'a'))
    a.start()
    assert asleep.wait(5)
    with chat._rate_lock:
        chat._global_until = 105.0  # another caller met a longer global limit
    release.set()
    a.join(5)
    assert posted_at and posted_at[0] >= 105.0


def test_the_wait_cap_is_measured_on_the_clock(chat):
    slept, clock = ticking(chat)
    chat._global_until = 100.0 + dc.MAX_RATE_WAIT / 2
    chat._http = FakeHTTP([Resp(200)])
    original = chat._sleep

    def sleep(seconds):
        original(seconds)
        if len(slept) > 50:  # bounded: without the cap this would never end
            chat._global_until = 0.0
            return
        chat._global_until = clock['now'] + dc.MAX_RATE_WAIT / 2  # always extended

    chat._sleep = sleep
    assert chat.send(CHAN, 'hi') is False
    assert chat._http.calls == [] and len(slept) <= 50
    assert clock['now'] - 100.0 <= dc.MAX_RATE_WAIT


def test_global_flag_in_headers_counts_too(chat):
    sleeps, _ = ticking(chat)
    chat._http = FakeHTTP([Resp(429, None, {'Retry-After': '3', 'X-RateLimit-Global': 'true'}),
                           Resp(200)])
    assert chat.send(CHAN, 'a') is True
    assert sleeps == [3.0]
    assert chat._global_until == 103.0 and chat._rate_until == {}


def test_a_bucket_429_is_remembered_for_that_channel_only(chat):
    ticking(chat)
    chat._http = FakeHTTP([Resp(429, {'retry_after': 1.5, 'global': False}), Resp(200)])
    assert chat.send(CHAN, 'a') is True
    assert chat._rate_until == {CHAN: 101.5} and chat._global_until == 0.0


def test_same_channel_sends_are_serialised(chat):
    sleeps, _ = ticking(chat)
    in_flight, release = threading.Event(), threading.Event()
    exhausted = {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset-After': '2.0'}

    class Blocking(FakeHTTP):
        def post(self, url, **kw):
            with self.lock:
                first = not self.calls
                self.calls.append((url, kw))
            if first:
                in_flight.set()
                release.wait(5)
                return Resp(200, None, exhausted)
            return Resp(200)

    chat._http = Blocking()
    a = threading.Thread(target=chat.send, args=(CHAN, 'a'))
    b = threading.Thread(target=chat.send, args=(CHAN, 'b'))
    a.start()
    assert in_flight.wait(5)
    b.start()
    try:
        # B must not slip past the bucket check while A's request is in flight.
        time.sleep(0.3)
        assert len(chat._http.calls) == 1
    finally:
        release.set()
        a.join(5)
        b.join(5)
    assert len(chat._http.calls) == 2
    assert sleeps == [2.0]


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), float('-inf'), 'nan', 'inf'])
def test_non_finite_retry_after_is_rejected(chat, bad):
    sleeps, _ = ticking(chat)
    chat._http = FakeHTTP([Resp(429, {'retry_after': bad}), Resp(200)])
    assert chat.send(CHAN, 'hi') is False
    assert sleeps == [] and len(chat._http.calls) == 1
    # And nothing poisoned: the next send is not held up forever.
    assert chat.send(CHAN, 'hi') is True
    assert sleeps == []


def test_non_finite_reset_after_does_not_wedge_the_bucket(chat):
    sleeps, _ = ticking(chat)
    chat._http = FakeHTTP([
        Resp(200, None, {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset-After': 'inf'}),
        Resp(200)])
    assert chat.send(CHAN, 'a') and chat.send(CHAN, 'b')
    assert sleeps == []


def test_connect_refuses_a_handshake_that_is_not_an_upgrade(chat, monkeypatch):
    """websocket-client 1.9.0 calls a socket connected after a 3xx reply when
    redirect_limit is 0; _connect must refuse it, and close it, on any version."""
    class Redirected(FakeWS):
        closed = False

        def getstatus(self):
            return 302

        def close(self, *args, **kwargs):
            self.closed = True

    ws = Redirected([])
    monkeypatch.setattr(dc.websocket, 'create_connection', lambda url, **kw: ws)
    with pytest.raises(websocket.WebSocketException):
        chat._connect(dc.GATEWAY_URL)
    assert ws.closed is True


# -- foreign code never takes the worker down or leaks a token ------------

def hook_excepthook(monkeypatch):
    escaped = []
    monkeypatch.setattr(threading, 'excepthook', escaped.append)
    return escaped


def raising_with_context():
    try:
        raise OSError(f'transport said {FAKE_TOKEN}')
    except OSError as inner:
        raise BaseException(f'handler said {FAKE_TOKEN}') from inner


def test_handler_base_exception_is_contained_and_the_next_event_is_delivered(chat, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    got = []

    def handler(event):
        if not got:
            got.append(event)
            raising_with_context()
        got.append(event)

    chat._handler = handler
    run_session(chat, [
        text(10, HELLO),
        text(0, msg(), s=2, t='MESSAGE_CREATE'),
        text(0, msg(id='400000000000000002'), s=3, t='MESSAGE_CREATE'),
    ])
    assert [e.message_id for e in got] == ['400000000000000001', '400000000000000002']
    assert 'BaseException' in caplog.text
    out = capsys.readouterr()
    assert FAKE_TOKEN not in caplog.text + out.out + out.err


def test_base_exception_in_a_live_worker_keeps_it_running(chat, monkeypatch, capsys):
    escaped = hook_excepthook(monkeypatch)
    seen = []

    def handler(event):
        seen.append(event)
        if len(seen) == 1:
            raising_with_context()

    ws = FakeWS([text(10, HELLO), text(0, msg(), s=2, t='MESSAGE_CREATE'),
                 text(0, msg(id='400000000000000002'), s=3, t='MESSAGE_CREATE')],
                on_empty=lambda: time.sleep(0.01))
    chat._connect = lambda url: ws
    chat.start(handler)
    try:
        assert wait_for(lambda: len(seen) == 2)
        assert chat._thread.is_alive() and chat.is_available() is True
    finally:
        chat.stop()
    out = capsys.readouterr()
    assert escaped == [] and FAKE_TOKEN not in out.out + out.err


def test_worker_top_level_failure_surfaces_as_unavailable_and_heals(chat, monkeypatch, caplog, capsys):
    escaped = hook_excepthook(monkeypatch)
    caplog.set_level(logging.DEBUG)

    def boom():
        raising_with_context()

    chat._session = boom
    chat.start(lambda e: None)
    chat._thread.join(5)
    assert not chat._thread.is_alive() and escaped == []
    assert chat._fatal and 'BaseException' in chat._fatal
    assert chat.is_available() is False
    out = capsys.readouterr()
    assert FAKE_TOKEN not in caplog.text + chat._fatal + out.out + out.err
    del chat._session
    chat._connect = lambda url: FakeWS([text(10, HELLO)], on_empty=lambda: time.sleep(0.01))
    chat.start(lambda e: None)  # the documented restart clears the failure
    try:
        assert chat._fatal is None and chat._thread.is_alive()
    finally:
        chat.stop()


def test_send_contains_base_exceptions(chat, caplog):
    caplog.set_level(logging.DEBUG)
    chat._http = FakeHTTP([BaseException(f'boom {FAKE_TOKEN}')])
    assert chat.send(CHAN, 'hi') is False
    assert FAKE_TOKEN not in caplog.text and 'BaseException' in caplog.text


# -- one atomic configuration snapshot ------------------------------------

def test_a_reload_never_widens_the_filter_while_it_is_in_flight(chat, monkeypatch):
    monkeypatch.setenv('DISCORD_GUILD_ID', GUILD)
    chat._load_config()
    real = dc.platform_secret
    paused, go = threading.Event(), threading.Event()

    def slow(platform, key, *args, **kwargs):
        if key == 'moderator_role_ids':  # the last setting read: parsed but not yet published
            paused.set()
            go.wait(5)
        return real(platform, key, *args, **kwargs)

    monkeypatch.setattr(dc, 'platform_secret', slow)
    outsider = msg(guild_id='800000000000000009')
    reload_ = threading.Thread(target=chat._load_config)
    reload_.start()
    assert paused.wait(5)
    try:
        assert chat.to_event(outsider) is None
        assert chat.to_event(msg()) is not None
    finally:
        go.set()
        reload_.join(5)
    assert chat.to_event(outsider) is None
    assert chat.to_event(msg()) is not None


def test_a_reload_that_adds_an_error_is_all_or_nothing(chat, monkeypatch):
    before = chat._cfg
    monkeypatch.setenv('DISCORD_GUILD_ID', 'oops')
    after = chat._load_config()
    assert after is chat._cfg and after is not before
    assert before.error is None and after.error and after.guild_id is None


def test_the_snapshot_does_not_print_the_token():
    assert FAKE_TOKEN not in repr(dc._Config(token=FAKE_TOKEN))


def test_a_different_reloaded_token_never_resumes_the_old_session(chat, monkeypatch):
    chat._session_id, chat._resume_url, chat._seq = 'sess', RESUME, 5
    chat._active = chat._cfg
    monkeypatch.setenv('DISCORD_BOT_TOKEN', 'SYNTHETIC-NEW-bbbbbbbb')
    chat.is_available()
    urls = []
    ws = FakeWS([text(10, HELLO)])
    chat._connect = lambda url: urls.append(url) or ws
    chat._wait = lambda s: False
    chat._stop_event.clear()
    chat._session()
    assert urls and urls[0].startswith('wss://gateway.discord.gg/')
    assert ws.ops() == [2] and ws.sent[0]['d']['token'] == 'SYNTHETIC-NEW-bbbbbbbb'
    assert FAKE_TOKEN not in json.dumps(ws.sent)


def test_a_reload_of_the_same_token_still_resumes(chat):
    chat._session_id, chat._resume_url, chat._seq = 'sess', RESUME, 5
    chat._active = chat._cfg
    chat.is_available()
    chat.is_available()
    ws = run_session(chat, [text(10, HELLO)])
    assert ws.ops() == [6]


# -- the automatic close acknowledgement ----------------------------------

def sent_close_status(resumable):
    """Feed a real reader a server close frame; decode what it transmitted."""
    ws, far = real_ws(max_bytes=1000)
    try:
        ws.resumable = resumable
        far.sendall(bytes([0x88, 2]) + struct.pack('!H', 1001))
        opcode, frame = ws.recv_data_frame(True)
        assert opcode == 8
        far.settimeout(2)
        raw = far.recv(64)
        assert raw[0] == 0x88 and raw[1] & 0x80  # a masked close frame
        length, mask = raw[1] & 0x7F, raw[2:6]
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(raw[6:6 + length]))
        return struct.unpack('!H', body[:2])[0]
    finally:
        ws.sock.close()
        far.close()


def test_the_libraries_own_close_ack_keeps_a_resumable_session():
    assert sent_close_status(True) == dc.RESUMABLE_CLOSE_CODE


def test_the_libraries_own_close_ack_is_orderly_when_not_resumable():
    assert sent_close_status(False) == dc.ORDERLY_CLOSE_CODE


def test_the_session_tells_the_reader_whether_a_close_is_resumable(chat):
    seen = []

    class Watching(FakeWS):
        def recv_data_frame(self, control_frame):
            seen.append(getattr(self, 'resumable', None))
            return super().recv_data_frame(control_frame)

    ws = Watching([text(10, HELLO), text(0, READY_BODY, s=1, t='READY'),
                   lambda: (chat._stop_event.set(), text(11))[1]])
    chat._connect = lambda url: ws
    chat._wait = lambda s: False
    chat._stop_event.clear()
    chat._session()
    assert seen == [False, False, True]


# -- the reader is probed, and fails closed -------------------------------

def test_the_installed_reader_passes_its_probe():
    assert dc._reader_error() is None


def test_a_missing_or_renamed_hook_is_reported(monkeypatch):
    from websocket import _abnf
    monkeypatch.delattr(_abnf.frame_buffer, 'recv_length')
    cls, reason = dc._make_bounded_websocket()
    assert cls is None and 'recv_length' in reason


def test_a_non_integer_length_fails_closed(monkeypatch):
    from websocket import _abnf
    original = _abnf.frame_buffer.recv_length

    def odd(self):
        original(self)
        self.length = '5'

    monkeypatch.setattr(_abnf.frame_buffer, 'recv_length', odd)
    cls, reason = dc._make_bounded_websocket()
    assert cls is not None and dc._probe_reader(cls)
    # And the reader itself refuses, even an ordinary frame, rather than trust it.
    near, far = socket.socketpair()
    try:
        near.settimeout(0.5)
        ws = cls(max_bytes=100)
        ws.sock = near
        ws.connected = True
        far.sendall(frame_header(5) + b'hello')
        with pytest.raises(dc.MessageTooLarge):
            ws.recv_data_frame(True)
    finally:
        near.close()
        far.close()


def test_a_trusting_reader_fails_the_probe():
    assert 'not refused' in dc._probe_reader(websocket.WebSocket)


@pytest.mark.parametrize('broken', ['no_class', 'probe_fails'])
def test_an_unusable_reader_means_unavailable_and_no_connection(chat, monkeypatch, caplog, broken):
    if broken == 'no_class':
        monkeypatch.setattr(dc, '_BoundedWebSocket', None)
        monkeypatch.setattr(dc, '_BOUNDED_ERROR', 'frame_buffer.recv_length is missing')
    else:
        monkeypatch.setattr(dc, '_probe_reader', lambda cls: 'an oversize frame was accepted')
    monkeypatch.setattr(dc, '_reader_cache', None)
    caplog.set_level(logging.WARNING)
    connects = []
    chat._connect = connects.append
    assert chat.is_available() is False and chat.is_available() is False
    assert len([r for r in caplog.records if 'size-bounded reader' in r.getMessage()]) == 1
    assert websocket.__version__ in caplog.text
    chat.start(lambda e: None)
    assert chat._thread is None and connects == []
    with pytest.raises(OSError):
        DiscordChat._connect(chat, dc.GATEWAY_URL)


# -- DISCORD_MAX_EVENT_BYTES ----------------------------------------------

def test_default_event_limit_is_two_mib(chat):
    assert chat._cfg.max_event_bytes == 2 * 1024 * 1024


@pytest.mark.parametrize('value', ['4096', str(dc.MAX_EVENT_BYTES_CEILING), ' 1048576 '])
def test_event_limit_setting_is_read(chat, monkeypatch, value):
    monkeypatch.setenv('DISCORD_MAX_EVENT_BYTES', value)
    assert chat.is_available() is True
    assert chat._cfg.max_event_bytes == int(value)


@pytest.mark.parametrize('value', ['0', '-5', 'big', '1.5', '2MiB', '٣٤',
                                   str(dc.MAX_EVENT_BYTES_CEILING + 1), '9' * 30])
def test_invalid_event_limit_fails_closed_naming_the_setting(chat, monkeypatch, caplog, value):
    monkeypatch.setenv('DISCORD_MAX_EVENT_BYTES', value)
    caplog.set_level(logging.DEBUG)
    assert chat.is_available() is False
    assert 'DISCORD_MAX_EVENT_BYTES' in caplog.text
    assert len(value) < 4 or value not in caplog.text
    assert chat.to_event(msg()) is None


def test_an_event_over_the_cap_stops_the_receiver_instead_of_replaying(chat, monkeypatch, caplog):
    monkeypatch.setenv('DISCORD_MAX_EVENT_BYTES', '1000')
    chat._load_config()
    caplog.set_level(logging.WARNING)
    connects = []
    big = (1, Frame(b'x' * 1001))
    chat._seq = None

    def connect(url):
        connects.append(url)
        return FakeWS([text(10, HELLO), text(0, READY_BODY, s=1, t='READY'), big])

    chat._connect = connect
    chat._wait = lambda s: pytest.fail('a stopped receiver must not wait to reconnect')
    chat._run()
    assert len(connects) == 1
    assert chat._fatal and 'DISCORD_MAX_EVENT_BYTES' in chat._fatal and 'raise' in chat._fatal
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1
    assert chat.is_available() is False
    assert chat._seq == 1  # the oversize event's sequence number was never taken


def test_a_real_frame_over_the_configured_cap_is_refused_before_its_payload():
    ws, far = real_ws(max_bytes=1000)
    try:
        far.sendall(frame_header(1001) + b'x' * 50)
        with pytest.raises(dc.MessageTooLarge):
            ws.recv_data_frame(True)
        far.setblocking(False)
        assert ws.sock.recv(100, socket.MSG_PEEK) == b'x' * 50
    finally:
        ws.sock.close()
        far.close()


# -- striped send locks ---------------------------------------------------

def test_a_channels_lock_keeps_its_identity_across_many_other_channels(chat):
    chat._clock = lambda: 100.0
    in_flight, release = threading.Event(), threading.Event()

    class Blocking(FakeHTTP):
        def post(self, url, **kw):
            with self.lock:
                first = not self.calls
                self.calls.append((url, kw))
            if first:
                in_flight.set()
                release.wait(5)
            return Resp(200)

    chat._http = Blocking()
    a = threading.Thread(target=chat.send, args=(CHAN, 'a'))
    a.start()
    assert in_flight.wait(5)
    others = [str(700000000100000 + i) for i in range(400)]
    others = [c for c in others if int(c) % dc.LOCK_STRIPES != int(CHAN) % dc.LOCK_STRIPES]
    for channel in others:  # far more than any old eviction threshold
        assert chat.send(channel, 'x') is True
    b = threading.Thread(target=chat.send, args=(CHAN, 'b'))
    b.start()
    try:
        time.sleep(0.3)
        assert len(chat._http.calls) == 1 + len(others)  # B is still waiting behind A
    finally:
        release.set()
        a.join(5)
        b.join(5)
    assert len(chat._http.calls) == 2 + len(others)
