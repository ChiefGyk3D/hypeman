# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Discord chat: hear guild text channels through the Gateway, reply over REST.

``discord.py`` (the webhook sender) only shouts. This is the other half: a bot
user that receives ``MESSAGE_CREATE`` events and turns each into one
``ChatEvent``. It holds no policy; who the bot is and when it speaks belong to
the consumer.

Decisions that matter, and why:

* Direct messages are dropped (no ``guild_id``). A guild bot has no business
  reading private conversations, and the bridge only relays guild channels.
* ``author_id`` is the Discord user id, never a name.
* ``bridged`` is True when the message carries a ``webhook_id`` (the usual way
  a bridge relays a foreign user) or when the author is a bot that is not this
  bot (a ghost or puppet). The raw hints go in ``extra`` so a consumer can
  decide differently. It is a heuristic, not proof: see docs/DISCORD_CHAT.md.
* Roles come only from configured ids (``moderator_role_ids``) and the guild
  owner id the gateway reports, and a bridged message never carries any: a
  relayed sender must not inherit a trust level from a ghost account.
* The token never appears in a log line or an exception message.
"""

import json
import logging
import random
import sys
import threading
import time
from typing import Any, Dict, FrozenSet, Optional, Set

import requests

from hypeman_social.social.base import platform_secret
from hypeman_social.social.events import (
    ROLE_BROADCASTER,
    ROLE_MODERATOR,
    ChatEvent,
    ChatHandler,
    ChatReceiver,
)

try:
    import websocket
except ImportError:
    websocket = None

logger = logging.getLogger(__name__)

API_BASE = 'https://discord.com/api/v10'
GATEWAY_URL = 'wss://gateway.discord.gg/?v=10&encoding=json'

# Gateway opcodes.
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# Intents: GUILDS (1<<0), GUILD_MESSAGES (1<<9), MESSAGE_CONTENT (1<<15, privileged).
INTENT_GUILDS = 1 << 0
INTENT_GUILD_MESSAGES = 1 << 9
INTENT_MESSAGE_CONTENT = 1 << 15
INTENTS = INTENT_GUILDS | INTENT_GUILD_MESSAGES | INTENT_MESSAGE_CONTENT

#: Close codes after which reconnecting cannot work until the operator acts.
FATAL_CLOSE_CODES: Dict[int, str] = {
    4004: 'authentication failed: the bot token was rejected',
    4010: 'invalid shard',
    4011: 'sharding required',
    4012: 'invalid gateway API version',
    4013: 'invalid intents',
    4014: ('a privileged intent was rejected: enable "Message Content Intent" on the '
           'application\'s Bot page in the Discord Developer Portal'),
}
#: Close codes after which the session is gone and a fresh Identify is needed.
SESSION_LOST_CLOSE_CODES = (4007, 4009)

#: Message types worth hearing: DEFAULT and REPLY. Joins, pins etc. are dropped.
HEARTABLE_TYPES = (0, 19)

MESSAGE_LIMIT = 2000
MAX_SEND_ATTEMPTS = 3
#: The longest a single send() will wait out rate limits in total, in seconds.
MAX_RATE_WAIT = 30.0
REQUEST_TIMEOUT = 10
MAX_BACKOFF = 300.0
POLL_SECONDS = 1.0

OPCODE_TEXT = 1
OPCODE_CLOSE = 8


def _split_ids(raw: Optional[str]) -> FrozenSet[str]:
    """Comma-separated snowflakes to a set; anything that is not digits is dropped."""
    found: Set[str] = set()
    for item in (raw or '').split(','):
        part = item.strip()
        if part.isdigit():
            found.add(part)
        elif part:
            logger.warning('Discord chat: ignoring a configured id that is not numeric')
    return frozenset(found)


class DiscordChat(ChatReceiver):
    """Receive guild chat from the Discord Gateway and reply over REST."""

    name = 'discord'

    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._guild_id: Optional[str] = None
        self._channel_ids: FrozenSet[str] = frozenset()
        self._mod_role_ids: FrozenSet[str] = frozenset()
        self._load_config()

        self._handler: Optional[ChatHandler] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._fatal: Optional[str] = None

        # Session state, owned by the gateway thread.
        self._session_id: Optional[str] = None
        self._resume_url: Optional[str] = None
        self._seq: Optional[int] = None
        self._bot_id: Optional[str] = None
        self._owners: Dict[str, str] = {}
        self._interval = 0.0
        self._next_beat = 0.0
        self._acked = True
        self._backoff = 1.0

        # Seams for tests.
        self._clock = time.monotonic
        self._sleep = time.sleep
        self._jitter = random.random
        self._http: Any = None
        self._rate_lock = threading.Lock()
        self._rate_until: Dict[str, float] = {}

    # -- configuration -----------------------------------------------------

    def _load_config(self) -> None:
        """Read config; called again from is_available() so recovery can heal."""
        self._token = platform_secret('Discord', 'bot_token') or None
        guild = (platform_secret('Discord', 'guild_id') or '').strip()
        self._guild_id = guild if guild.isdigit() else None
        self._channel_ids = _split_ids(platform_secret('Discord', 'channel_ids'))
        self._mod_role_ids = _split_ids(platform_secret('Discord', 'moderator_role_ids'))

    def _redact(self, text: str) -> str:
        if self._token:
            text = text.replace(self._token, '[redacted]')
        return text

    def _describe(self, exc: BaseException) -> str:
        return f'{type(exc).__name__}: {self._redact(str(exc))}'

    # -- ChatReceiver ------------------------------------------------------

    def is_available(self) -> bool:
        self._load_config()
        if websocket is None:
            logger.warning('Discord chat needs websocket-client: pip install "hypeman-social[discord-chat]"')
            return False
        if not self._token:
            logger.warning('Discord chat: no bot_token configured')
            return False
        if self._fatal:
            logger.warning('Discord chat is stopped: %s', self._fatal)
            return False
        return True

    def start(self, handler: ChatHandler) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if not self.is_available():
            return
        self._handler = handler
        self._fatal = None
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name='discord-chat', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._thread = None

    # -- gateway loop ------------------------------------------------------

    def _connect(self, url: str) -> Any:
        return websocket.create_connection(url, timeout=POLL_SECONDS)

    def _wait(self, seconds: float) -> bool:
        """Sleep between connection attempts; True if asked to stop meanwhile."""
        return self._stop_event.wait(seconds)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._session()
            except Exception as exc:
                logger.warning('Discord gateway error: %s', self._describe(exc))
            if self._fatal or self._stop_event.is_set():
                break
            if self._wait(self._backoff):
                break
            self._backoff = min(self._backoff * 2, MAX_BACKOFF)

    def _send_frame(self, ws: Any, payload: Dict[str, Any]) -> None:
        ws.send(json.dumps(payload))

    def _heartbeat(self, ws: Any) -> None:
        self._send_frame(ws, {'op': OP_HEARTBEAT, 'd': self._seq})
        self._acked = False

    def _session(self) -> None:
        """One connection, from connect to disconnect. Returns when it ends."""
        resuming = bool(self._session_id and self._resume_url and self._seq is not None)
        if resuming:
            base = self._resume_url or ''
            url = base + ('&' if '?' in base else '?') + 'v=10&encoding=json'
        else:
            self._reset_session()
            url = GATEWAY_URL
        ws = self._connect(url)
        self._acked = True
        self._next_beat = float('inf')
        try:
            while not self._stop_event.is_set():
                if self._clock() >= self._next_beat:
                    if not self._acked:
                        # No ACK since the last beat: the connection is a zombie.
                        logger.warning('Discord gateway: heartbeat not acknowledged, reconnecting')
                        ws.close(status=4000)
                        return
                    self._heartbeat(ws)
                    self._next_beat = self._clock() + self._interval
                try:
                    opcode, frame = ws.recv_data_frame(True)
                except Exception as exc:
                    if websocket is not None and isinstance(exc, websocket.WebSocketTimeoutException):
                        continue
                    logger.info('Discord gateway closed: %s', type(exc).__name__)
                    return
                if opcode == OPCODE_CLOSE:
                    self._on_close(frame)
                    return
                if opcode != OPCODE_TEXT:
                    continue
                if not self._on_payload(ws, frame):
                    return
        finally:
            try:
                ws.close()
            except Exception as exc:
                logger.debug('Discord gateway close failed: %s', type(exc).__name__)

    def _reset_session(self) -> None:
        self._session_id = None
        self._resume_url = None
        self._seq = None

    def _on_close(self, frame: Any) -> None:
        data = getattr(frame, 'data', b'') or b''
        code = int.from_bytes(data[:2], 'big') if len(data) >= 2 else None
        if code in FATAL_CLOSE_CODES:
            self._fatal = FATAL_CLOSE_CODES[code]
            logger.error('Discord gateway closed (%s): %s', code, self._fatal)
        elif code in SESSION_LOST_CLOSE_CODES:
            self._reset_session()
            logger.warning('Discord gateway closed (%s): starting a new session', code)
        else:
            logger.info('Discord gateway closed (%s): will resume', code)

    def _on_payload(self, ws: Any, raw: Any) -> bool:
        """Handle one gateway frame. False ends this connection."""
        data = getattr(raw, 'data', raw)
        try:
            msg = json.loads(data)
        except (ValueError, TypeError):
            logger.warning('Discord gateway sent a frame that is not JSON')
            return True
        if not isinstance(msg, dict):
            return True
        op = msg.get('op')
        seq = msg.get('s')
        if isinstance(seq, int):
            self._seq = seq
        if op == OP_HELLO:
            body = msg.get('d') or {}
            self._interval = float(body.get('heartbeat_interval', 41250)) / 1000.0
            self._next_beat = self._clock() + self._interval * self._jitter()
            self._identify_or_resume(ws)
        elif op == OP_HEARTBEAT_ACK:
            self._acked = True
        elif op == OP_HEARTBEAT:
            self._heartbeat(ws)
        elif op == OP_RECONNECT:
            return False
        elif op == OP_INVALID_SESSION:
            if msg.get('d') is not True:
                self._reset_session()
                # Discord asks for a short random pause before a fresh Identify.
                self._wait(1.0 + 4.0 * self._jitter())
            return False
        elif op == OP_DISPATCH:
            self._dispatch(msg.get('t'), msg.get('d'))
        return True

    def _identify_or_resume(self, ws: Any) -> None:
        if self._session_id and self._seq is not None:
            self._send_frame(ws, {'op': OP_RESUME, 'd': {
                'token': self._token, 'session_id': self._session_id, 'seq': self._seq}})
            return
        self._send_frame(ws, {'op': OP_IDENTIFY, 'd': {
            'token': self._token,
            'intents': INTENTS,
            'properties': {'os': sys.platform, 'browser': 'hypeman', 'device': 'hypeman'},
        }})

    def _dispatch(self, kind: Optional[str], body: Any) -> None:
        if not isinstance(body, dict):
            return
        if kind == 'READY':
            self._session_id = str(body.get('session_id') or '') or None
            self._resume_url = body.get('resume_gateway_url')
            user = body.get('user') or {}
            self._bot_id = str(user.get('id')) if user.get('id') is not None else None
            self._backoff = 1.0
            logger.info('Discord gateway ready')
        elif kind == 'RESUMED':
            self._backoff = 1.0
            logger.info('Discord gateway resumed')
        elif kind == 'GUILD_CREATE':
            if body.get('id') is not None and body.get('owner_id') is not None:
                self._owners[str(body['id'])] = str(body['owner_id'])
        elif kind == 'MESSAGE_CREATE':
            event = self.to_event(body)
            if event is not None and self._handler is not None:
                try:
                    self._handler(event)
                except Exception as exc:
                    logger.warning('Discord chat handler raised: %s', self._describe(exc))

    # -- conversion --------------------------------------------------------

    def to_event(self, msg: Dict[str, Any]) -> Optional[ChatEvent]:
        """Convert a MESSAGE_CREATE body, or None when it is not ours to hear."""
        guild_id = msg.get('guild_id')
        if guild_id is None:
            return None  # a direct message: never read
        guild_id = str(guild_id)
        channel_id = str(msg.get('channel_id', ''))
        if self._guild_id and guild_id != self._guild_id:
            return None
        if self._channel_ids and channel_id not in self._channel_ids:
            return None
        if msg.get('type', 0) not in HEARTABLE_TYPES:
            return None
        author = msg.get('author')
        if not isinstance(author, dict) or author.get('id') is None or msg.get('id') is None:
            return None
        text = msg.get('content') or ''
        if not text:
            return None

        author_id = str(author['id'])
        is_self = self._bot_id is not None and author_id == self._bot_id
        webhook_id = msg.get('webhook_id')
        author_is_bot = bool(author.get('bot'))
        bridged = (webhook_id is not None) or (author_is_bot and not is_self)

        member = msg.get('member') or {}
        roles: Set[str] = set()
        if not bridged:
            if self._owners.get(guild_id) == author_id:
                roles.add(ROLE_BROADCASTER)
            if self._mod_role_ids & {str(r) for r in (member.get('roles') or [])}:
                roles.add(ROLE_MODERATOR)

        ref = msg.get('message_reference') or {}
        reply_to = ref.get('message_id') if msg.get('type') == 19 else None
        name = (member.get('nick') or author.get('global_name')
                or author.get('username') or author_id)
        extra = {
            'guild_id': guild_id,
            'author_username': str(author.get('username', '')),
            'author_bot': 'true' if author_is_bot else 'false',
        }
        if webhook_id is not None:
            extra['webhook_id'] = str(webhook_id)
        return ChatEvent(
            platform=self.name,
            channel=channel_id,
            message_id=str(msg['id']),
            author_id=author_id,
            author_name=str(name),
            text=str(text),
            is_self=is_self,
            bridged=bridged,
            roles=frozenset(roles),
            reply_to_id=str(reply_to) if reply_to is not None else None,
            extra=extra,
        )

    # -- sending -----------------------------------------------------------

    def _session_http(self) -> Any:
        if self._http is None:
            self._http = requests.Session()
        return self._http

    def send(self, channel: str, text: str, reply_to_id: Optional[str] = None) -> bool:
        """Post ``text`` to a channel id. Truncated to 2000 characters. Never raises."""
        try:
            return self._send(channel, text, reply_to_id)
        except Exception as exc:
            logger.warning('Discord send failed: %s', self._describe(exc))
            return False

    def _send(self, channel: str, text: str, reply_to_id: Optional[str]) -> bool:
        if not self._token or not str(channel).isdigit() or not text:
            return False
        if len(text) > MESSAGE_LIMIT:
            text = text[:MESSAGE_LIMIT - 1] + '…'
        body: Dict[str, Any] = {
            'content': text,
            # Echoed user text must never ping @everyone, a role, or a user.
            'allowed_mentions': {'parse': []},
        }
        if reply_to_id and str(reply_to_id).isdigit():
            body['message_reference'] = {
                'message_id': str(reply_to_id), 'fail_if_not_exists': False}
        url = f'{API_BASE}/channels/{channel}/messages'
        headers = {
            'Authorization': f'Bot {self._token}',
            'User-Agent': 'DiscordBot (https://github.com/ChiefGyk3D/hypeman, 0)',
        }
        waited = 0.0
        for attempt in range(MAX_SEND_ATTEMPTS):
            with self._rate_lock:
                pause = self._rate_until.get(channel, 0.0) - self._clock()
            if pause > 0:
                if waited + pause > MAX_RATE_WAIT:
                    logger.warning('Discord send: rate limited for longer than the cap')
                    return False
                self._sleep(pause)
                waited += pause
            resp = self._session_http().post(
                url, json=body, headers=headers, timeout=REQUEST_TIMEOUT,
                allow_redirects=False)
            status = resp.status_code
            if status in (200, 201):
                self._note_bucket(channel, resp)
                return True
            if status == 429:
                delay = self._retry_after(resp)
                if waited + delay > MAX_RATE_WAIT or attempt == MAX_SEND_ATTEMPTS - 1:
                    logger.warning('Discord send: rate limited, giving up')
                    return False
                self._sleep(delay)
                waited += delay
                continue
            if status >= 500 and attempt < MAX_SEND_ATTEMPTS - 1:
                self._sleep(min(2.0 ** attempt, 4.0))
                waited += min(2.0 ** attempt, 4.0)
                continue
            logger.warning('Discord send rejected with HTTP %s', status)
            return False
        return False

    def _note_bucket(self, channel: str, resp: Any) -> None:
        """Remember an exhausted bucket so the next send waits instead of eating a 429."""
        headers = resp.headers or {}
        try:
            if str(headers.get('X-RateLimit-Remaining')) == '0':
                after = float(headers.get('X-RateLimit-Reset-After', 0))
                with self._rate_lock:
                    self._rate_until[channel] = self._clock() + max(0.0, after)
                    if len(self._rate_until) > 256:
                        now = self._clock()
                        self._rate_until = {k: v for k, v in self._rate_until.items() if v > now}
        except (TypeError, ValueError):
            return

    @staticmethod
    def _retry_after(resp: Any) -> float:
        value: Any = None
        try:
            value = resp.json().get('retry_after')
        except Exception:
            value = None
        if value is None:
            value = (resp.headers or {}).get('Retry-After')
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return 1.0


__all__ = ['DiscordChat', 'INTENTS', 'MESSAGE_LIMIT']
