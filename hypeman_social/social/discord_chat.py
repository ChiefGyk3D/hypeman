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
* The token never appears in a log line or an exception message: transport
  errors are logged by type only, never by their text.
* The token only ever goes to Discord: the initial gateway URL and every
  ``resume_gateway_url`` must pass ``is_trusted_gateway_url`` before a socket is
  opened, and handshake redirects are off.
* Inbound frames are size-capped in encoded bytes, per frame and per fragmented
  message, before the payload is read. That needs websocket-client internals, so
  the adapter probes them and fails closed (unavailable) if they do not behave.
  An event over the cap is a stopped condition, never a replay loop.
* Identify is rate-limited by us as well as by Discord: a valid GET /gateway/bot
  answer before every fresh Identify, 5 s spacing, a daily budget that refuses
  rather than burns the last Identifies, and a backoff that only resets after a
  connection stayed healthy. Foreign code (the handler) is fenced with
  ``except BaseException`` and logged by class name only.
"""

import json
import logging
import math
import random
import re
import secrets
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple
from urllib.parse import urlsplit

import requests

from hypeman_social.social.base import platform_secret
from hypeman_social.social.events import (
    ROLE_BROADCASTER,
    ROLE_MODERATOR,
    ChatEvent,
    ChatHandler,
    ChatReceiver,
)

websocket: Any = None
try:
    import websocket as _websocket_module
except ImportError:
    pass
else:
    websocket = _websocket_module

logger = logging.getLogger(__name__)

API_BASE = 'https://discord.com/api/v10'
GATEWAY_URL = 'wss://gateway.discord.gg/?v=10&encoding=json'
_GATEWAY_QUERY = 'v=10&encoding=json'
USER_AGENT = 'DiscordBot (https://github.com/ChiefGyk3D/hypeman, 0)'

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
#: A reset_after longer than this is stored capped; send() gives up on it anyway.
MAX_BUCKET_WAIT = 3600.0

#: Largest inbound frame, and largest reassembled message, in encoded bytes.
#: Discord documents no inbound maximum; a GUILD_CREATE for a very large guild is
#: the biggest legitimate payload. DISCORD_MAX_EVENT_BYTES raises it, up to the
#: ceiling (the whole message is held in memory while it is read).
MAX_MESSAGE_BYTES = 2 * 1024 * 1024
MAX_EVENT_BYTES_CEILING = 64 * 1024 * 1024

#: Close statuses. Discord invalidates the session on 1000 and 1001 only, so a
#: reconnect that wants to Resume must close with something else.
RESUMABLE_CLOSE_CODE = 4000
OVERSIZE_CLOSE_CODE = 1009
ORDERLY_CLOSE_CODE = 1000

#: A connection must stay up this long after READY or RESUMED before the failure
#: backoff is forgotten. Without it a loop of Identify, READY, session lost
#: would run at the minimum backoff and burn the daily Identify allowance.
STABLE_SECONDS = 60.0
#: Seconds between this client's Identifies. Always 5, whatever max_concurrency
#: says: an unsharded client is always in bucket 0 (shard_id % max_concurrency).
IDENTIFY_SPACING_SECONDS = 5.0
#: Discord's documented allowance per 24 hours (overridden by GET /gateway/bot).
DAILY_IDENTIFY_LIMIT = 1000
#: Identifies never spent, so a restart loop cannot exhaust the allowance.
IDENTIFY_RESERVE = 20
DAY_SECONDS = 86400.0

#: How long stop() waits for the worker, then again after closing the socket.
STOP_JOIN_SECONDS = 5.0
STOP_ABORT_SECONDS = 2.0

OPCODE_TEXT = 1
OPCODE_CLOSE = 8
#: Striped per-channel send locks: stable identity, bounded memory, no eviction.
LOCK_STRIPES = 64

_SNOWFLAKE = re.compile(r'[0-9]{1,20}')
_HOST = re.compile(r'[a-z0-9-]+(\.[a-z0-9-]+)*')
_GATEWAY_DOMAIN = '.discord.gg'


def _is_snowflake(value: Any) -> bool:
    """ASCII digits only: str.isdigit() also accepts other scripts' digits."""
    return isinstance(value, str) and _SNOWFLAKE.fullmatch(value) is not None


def is_trusted_gateway_url(url: Any) -> bool:
    """Whether a gateway or resume URL may receive the bot token.

    ``wss`` only; host ``gateway.discord.gg`` or a label-boundary subdomain of
    ``discord.gg`` (Discord's resume URLs are regional subdomains); no userinfo,
    fragment or control characters; port absent or 443.
    """
    if not isinstance(url, str) or not url:
        return False
    if any(ord(c) <= 32 or ord(c) == 127 or c == '\\' for c in url) or '#' in url:
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != 'wss' or '@' in parts.netloc:
        return False
    if parts.username is not None or parts.password is not None or parts.fragment:
        return False
    if port not in (None, 443):
        return False
    host = parts.hostname or ''
    return _HOST.fullmatch(host) is not None and host.endswith(_GATEWAY_DOMAIN)


class MessageTooLarge(Exception):
    """An inbound frame or message exceeded the configured event size."""


def _make_bounded_websocket() -> Tuple[Any, Optional[str]]:
    """(class, None) for a websocket-client WebSocket that refuses oversize frames
    up front, or (None, reason) when this websocket-client cannot support it.

    The stock reader trusts the length in a frame header and allocates for it
    (and for every fragment of a message) before any size check can run. The
    subclass checks the declared length straight after the header is read, so a
    frame or an aggregate over the limit is refused before its payload is
    received. This leans on websocket-client internals, so there is no fallback
    to the stock reader: ``_reader_error`` probes the hook and the adapter
    reports itself unavailable when it does not work. The same subclass makes the
    close acknowledgement the library sends by itself follow ``resumable``
    (Discord keeps a session only if the close status is not 1000 or 1001).
    """
    if websocket is None:
        return None, 'websocket-client is not installed'
    try:
        from websocket._abnf import ABNF, frame_buffer
    except ImportError:
        return None, 'websocket._abnf.frame_buffer cannot be imported'
    for attribute in ('recv_length', 'recv_frame'):
        if not callable(getattr(frame_buffer, attribute, None)):
            return None, f'frame_buffer.{attribute} is missing'

    class _BoundedFrameBuffer(frame_buffer):
        def __init__(self, *args: Any, limit: int = MAX_MESSAGE_BYTES, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.limit = limit
            self._message_bytes = 0

        def recv_length(self) -> None:
            super().recv_length()
            header: Any = self.header
            length: Any = self.length
            if header is None or not isinstance(length, int) or isinstance(length, bool):
                raise MessageTooLarge('frame length is unreadable')  # fail closed
            earlier = self._message_bytes if header[4] == ABNF.OPCODE_CONT else 0
            if length + earlier > self.limit:
                raise MessageTooLarge('frame over the size limit')

        def recv_frame(self) -> Any:
            frame = super().recv_frame()
            if frame.opcode in (ABNF.OPCODE_TEXT, ABNF.OPCODE_BINARY):
                self._message_bytes = 0 if frame.fin else len(frame.data)
            elif frame.opcode == ABNF.OPCODE_CONT:
                self._message_bytes = 0 if frame.fin else self._message_bytes + len(frame.data)
            return frame

    class _BoundedWebSocket(websocket.WebSocket):
        #: Set by the session before each read: whether a received close should
        #: be acknowledged in a way that keeps the session.
        resumable = False

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            limit = kwargs.pop('max_bytes', MAX_MESSAGE_BYTES)
            super().__init__(*args, **kwargs)
            self.frame_buffer = _BoundedFrameBuffer(
                self._recv, bool(kwargs.get('skip_utf8_validation', False)), limit=limit)

        def send_close(self, status: int = ORDERLY_CLOSE_CODE, reason: Any = b'') -> None:
            if status == ORDERLY_CLOSE_CODE and self.resumable:
                status = RESUMABLE_CLOSE_CODE
            super().send_close(status, reason)

    return _BoundedWebSocket, None


_BoundedWebSocket, _BOUNDED_ERROR = _make_bounded_websocket()
_reader_cache: Optional[Tuple[Optional[str]]] = None


def _probe_reader(cls: Any) -> Optional[str]:
    """Prove, on in-process socket pairs, that ``cls`` refuses a frame whose header
    declares more than its limit before the payload arrives (only a few payload
    bytes are ever sent, so a trusting reader times out), refuses a fragmented
    message whose total passes the limit, and still reads an ordinary frame.
    Returns a reason on failure, None on success."""
    def read(raw: bytes, expect: Optional[type]) -> Optional[str]:
        reader, writer = socket.socketpair()
        reader.settimeout(0.5)
        try:
            ws = cls(max_bytes=100)
            ws.sock = reader
            ws.connected = True
            writer.sendall(raw)
            try:
                got = ws.recv()
            except Exception as exc:
                return None if expect and isinstance(exc, expect) else type(exc).__name__
            return 'accepted' if expect else ('ok' if got == 'hello' else 'misread')
        finally:
            reader.close()
            writer.close()

    try:
        huge = struct.pack('!BBQ', 0x81, 127, 1 << 40) + b'x' * 8
        split = struct.pack('!BB', 0x01, 60) + b'a' * 60 + struct.pack('!BB', 0x80, 60) + b'b' * 8
        if read(huge, MessageTooLarge) is not None:
            return 'an oversize frame was not refused before its payload was read'
        if read(split, MessageTooLarge) is not None:
            return 'a fragmented message over the limit was not refused'
        if read(struct.pack('!BB', 0x81, 5) + b'hello', None) != 'ok':
            return 'an ordinary frame was not read correctly'
    except Exception:
        return 'the compatibility probe itself failed'
    return None


def _reader_error() -> Optional[str]:
    """None when the bounded reader exists and passed its probe, else a reason
    naming the installed websocket-client. Computed once per process."""
    global _reader_cache
    if _reader_cache is None:
        version = getattr(websocket, '__version__', 'unknown') if websocket else 'not installed'
        reason = _BOUNDED_ERROR or 'no bounded reader'
        if _BoundedWebSocket is not None:
            reason = _probe_reader(_BoundedWebSocket) or ''
        if reason:
            reason = (f'Discord chat disabled: websocket-client {version} cannot provide the '
                      f'size-bounded reader this adapter requires ({reason}). Install a tested '
                      f'version: pip install "websocket-client>=1.6.0,<2"')
        _reader_cache = (reason or None,)
    return _reader_cache[0]


@dataclass(frozen=True)
class _Config:
    """One immutable reading of the settings, published by a single assignment so
    no reader ever sees a half-applied reload."""

    token: Optional[str] = field(default=None, repr=False)
    guild_id: Optional[str] = None
    channel_ids: FrozenSet[str] = frozenset()
    mod_role_ids: FrozenSet[str] = frozenset()
    max_event_bytes: int = MAX_MESSAGE_BYTES
    error: Optional[str] = None
    #: Bumped whenever the token changes: a session belongs to one credential.
    generation: int = 0


def _split_ids(raw: str) -> Tuple[FrozenSet[str], bool]:
    """Comma-separated snowflakes to (ids, valid). Blank items are skipped."""
    named = [i for i in (part.strip() for part in raw.split(',')) if i]
    found = frozenset(i for i in named if _is_snowflake(i))
    return found, bool(named) and len(found) == len(set(named))


class DiscordChat(ChatReceiver):
    """Receive guild chat from the Discord Gateway and reply over REST."""

    name = 'discord'

    def __init__(self) -> None:
        self._cfg = _Config()
        self._cfg_lock = threading.Lock()
        self._load_config()
        self._active = self._cfg  # the snapshot the current session was started with

        self._handler: Optional[ChatHandler] = None
        self._thread: Optional[threading.Thread] = None
        self._lifecycle = threading.Lock()
        #: One per start(); only replaced when no worker is alive.
        self._stop_event = threading.Event()
        self._ws: Any = None
        self._fatal: Optional[str] = None
        self._reader_logged = False

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
        self._ready_at: Optional[float] = None
        self._identify_times: List[float] = []

        # Seams for tests.
        self._clock = time.monotonic
        self._sleep = time.sleep
        self._jitter = random.random
        self._http: Any = None

        # Send coordination. _rate_lock guards only the deadlines below; a request
        # holds its channel's stripe lock, never _rate_lock, across the network call.
        self._rate_lock = threading.Lock()
        self._rate_until: Dict[str, float] = {}
        self._global_until = 0.0
        self._channel_locks = tuple(threading.Lock() for _ in range(LOCK_STRIPES))

    # -- configuration -----------------------------------------------------

    def _load_config(self) -> _Config:
        """Read every setting into one snapshot and publish it with one assignment.

        An absent filter means unrestricted, by design. A setting that is
        supplied but invalid fails closed: the adapter is unavailable and the
        error names the setting, never the value. Called again from
        is_available() so recovery can heal.
        """
        with self._cfg_lock:
            token = platform_secret('Discord', 'bot_token') or None
            bad: List[str] = []

            guild: Optional[str] = (platform_secret('Discord', 'guild_id') or '').strip() or None
            if guild is not None and not _is_snowflake(guild):
                guild = None
                bad.append('DISCORD_GUILD_ID')

            channels: FrozenSet[str] = frozenset()
            raw_channels = (platform_secret('Discord', 'channel_ids') or '').strip()
            if raw_channels:
                channels, valid = _split_ids(raw_channels)
                if not valid:
                    channels = frozenset()
                    bad.append('DISCORD_CHANNEL_IDS')

            limit = MAX_MESSAGE_BYTES
            raw_limit = (platform_secret('Discord', 'max_event_bytes') or '').strip()
            if raw_limit:
                if re.fullmatch(r'[0-9]{1,10}', raw_limit) and \
                        0 < int(raw_limit) <= MAX_EVENT_BYTES_CEILING:
                    limit = int(raw_limit)
                else:
                    bad.append('DISCORD_MAX_EVENT_BYTES')

            roles, _ = _split_ids(platform_secret('Discord', 'moderator_role_ids') or '')
            # An invalid role id only removes a privilege, so it is dropped.
            self._cfg = _Config(
                token=token, guild_id=guild, channel_ids=channels, mod_role_ids=roles,
                max_event_bytes=limit,
                error=f'{" and ".join(bad)} is set but is not valid' if bad else None,
                generation=self._cfg.generation + (self._cfg.token != token))
            return self._cfg

    @staticmethod
    def _describe(exc: BaseException) -> str:
        """Log an exception by class name only: its text, args and chained context
        can carry a token (current or previous) and are not needed to act on."""
        return type(exc).__name__

    # -- ChatReceiver ------------------------------------------------------

    def is_available(self) -> bool:
        cfg = self._load_config()
        if websocket is None:
            logger.warning('Discord chat needs websocket-client: pip install "hypeman-social[discord-chat]"')
            return False
        error = _reader_error()
        if error is not None:
            if not self._reader_logged:
                self._reader_logged = True
                logger.error('%s', error)
            return False
        if not cfg.token:
            logger.warning('Discord chat: no bot credential configured')
            return False
        if cfg.error:
            logger.error('Discord chat: %s', cfg.error)
            return False
        if self._fatal:
            logger.warning('Discord chat is stopped: %s', self._fatal)
            return False
        return True

    def start(self, handler: ChatHandler) -> None:
        """Start the worker. Also the documented recovery from a fatal close:
        it clears the fatal latch and re-reads credentials before checking."""
        with self._lifecycle:
            if self._thread is not None:
                if self._thread.is_alive():
                    logger.warning('Discord chat: a worker is still running; not starting another')
                    return
                self._thread = None
            self._fatal = None
            if not self.is_available():
                return
            self._handler = handler
            self._stop_event = threading.Event()
            self._thread = threading.Thread(target=self._serve, name='discord-chat', daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stop the worker. See shutdown() for whether it really exited."""
        self.shutdown()

    def shutdown(self) -> bool:
        """Stop the worker. True when it has exited; False (and logged) when not.

        The thread reference is kept until the worker really exits, so start()
        cannot create a second receiver beside a stuck one.
        """
        with self._lifecycle:
            self._stop_event.set()
            thread = self._thread
            if thread is None:
                return True
            if thread is threading.current_thread():
                return False  # called from the handler: the worker exits on its own
            thread.join(timeout=STOP_JOIN_SECONDS)
            if thread.is_alive():
                self._abort_connection()
                thread.join(timeout=STOP_ABORT_SECONDS)
            if thread.is_alive():
                logger.error('Discord chat: the worker did not stop; start() will refuse '
                             'until it exits')
                return False
            self._thread = None
            return True

    def _abort_connection(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            abort = getattr(ws, 'abort', None) or ws.shutdown
            abort()
        except Exception as exc:
            logger.debug('Discord gateway abort failed: %s', self._describe(exc))

    # -- gateway loop ------------------------------------------------------

    def _connect(self, url: str) -> Any:
        # Never fall back to the stock reader: it checks sizes after allocating.
        if _reader_error() is not None:
            raise OSError('the size-bounded websocket reader is unavailable')
        # redirect_limit=0: a handshake redirect would carry the next frame
        # (Identify or Resume, with the token) to a host nobody checked.
        ws = websocket.create_connection(
            url, timeout=POLL_SECONDS, redirect_limit=0, class_=_BoundedWebSocket,
            max_bytes=self._active.max_event_bytes)
        # websocket-client 1.9.0 marks a socket connected after a 3xx handshake
        # reply when redirect_limit is 0 (1.9.2 raises); accept only a real upgrade.
        if ws.getstatus() != 101:
            ws.close()
            raise websocket.WebSocketException('Gateway handshake was not an upgrade')
        return ws

    def _wait(self, seconds: float) -> bool:
        """Sleep between connection attempts; True if asked to stop meanwhile."""
        return self._stop_event.wait(seconds)

    def _serve(self) -> None:
        """Thread body. Nothing may escape: the default thread excepthook prints
        an exception with its chained context, which can carry a token."""
        try:
            self._run()
        except BaseException as exc:
            self._fatal = f'the receive thread failed ({self._describe(exc)}); call start() to restart'
            logger.error('Discord chat worker failed: %s', self._describe(exc))

    def _run(self) -> None:
        stop = self._stop_event
        while not stop.is_set():
            self._ready_at = None
            try:
                self._session()
            except Exception as exc:
                logger.warning('Discord gateway error: %s', self._describe(exc))
            if self._fatal or stop.is_set():
                break
            if self._ready_at is not None and self._clock() - self._ready_at >= STABLE_SECONDS:
                self._backoff = 1.0  # it earned a clean slate; READY alone does not
            if self._wait(self._backoff):
                break
            self._backoff = min(self._backoff * 2, MAX_BACKOFF)

    def _send_frame(self, ws: Any, payload: Dict[str, Any]) -> None:
        ws.send(json.dumps(payload))

    def _heartbeat(self, ws: Any) -> None:
        self._send_frame(ws, {'op': OP_HEARTBEAT, 'd': self._seq})
        self._acked = False

    def _start_limit(self, token: str) -> Optional[Tuple[str, int, int]]:
        """GET /gateway/bot: (gateway url, total, remaining), or None unless Discord
        gave a complete, valid answer. Asked before every fresh Identify, so the
        budget is never guessed and no state about it is kept."""
        def number(value: Any, low: int) -> int:
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value) or value < low:
                raise ValueError('bad session_start_limit field')
            return int(value)

        try:
            resp = self._session_http().get(
                f'{API_BASE}/gateway/bot',
                headers={'Authorization': f'Bot {token}', 'User-Agent': USER_AGENT},
                timeout=REQUEST_TIMEOUT, allow_redirects=False)
            if resp.status_code != 200:
                logger.warning('Discord start-limit lookup returned HTTP %s', resp.status_code)
                return None
            data = resp.json()
            limit = data['session_start_limit']
            total = number(limit['total'], 1)
            remaining = number(limit['remaining'], 0)
            number(limit['reset_after'], 0)
            number(limit['max_concurrency'], 1)
            url = data.get('url')
            if isinstance(url, str) and is_trusted_gateway_url(url):
                url += ('&' if '?' in url else '?') + _GATEWAY_QUERY
            else:
                url = GATEWAY_URL
            return url, total, remaining
        except Exception as exc:
            logger.warning('Discord start-limit lookup failed: %s', self._describe(exc))
            return None

    def _identify_gate(self, token: str) -> Optional[str]:
        """The gateway URL to Identify at, or None when a fresh Identify must not
        be sent now: Discord's budget is unknown (the run loop retries with its
        backoff), nearly spent (a stopped condition), or we were asked to stop."""
        info = self._start_limit(token)
        if info is None:
            return None
        url, total, remaining = info
        now = self._clock()
        self._identify_times = [t for t in self._identify_times if now - t < DAY_SECONDS]
        if len(self._identify_times) >= total - IDENTIFY_RESERVE or remaining <= IDENTIFY_RESERVE:
            self._fatal = ('refusing to Identify: the daily session start allowance is nearly '
                           'used (a reserve is kept so the token is not reset)')
            logger.error('Discord chat: %s', self._fatal)
            return None
        if self._identify_times:
            delay = self._identify_times[-1] + IDENTIFY_SPACING_SECONDS - now
            if delay > 0 and self._wait(delay):
                return None
        return url

    def _too_large(self, cfg: _Config) -> None:
        """An event over the cap is never replayed or retried: stop, say how to fix."""
        self._fatal = (f'an incoming event was larger than {cfg.max_event_bytes} bytes; raise '
                       f'DISCORD_MAX_EVENT_BYTES (at most {MAX_EVENT_BYTES_CEILING}) to hear it')
        logger.error('Discord chat: %s', self._fatal)

    def _session(self) -> None:
        """One connection, from connect to disconnect. Returns when it ends."""
        stop = self._stop_event
        cfg = self._cfg
        token = cfg.token
        if not token:
            return
        url = GATEWAY_URL
        # A reloaded, different credential never resumes the previous one's session.
        resuming = (self._session_id is not None and self._seq is not None
                    and self._active.generation == cfg.generation)
        if resuming and not is_trusted_gateway_url(self._resume_url):
            logger.error('Discord gateway: ignoring an untrusted resume URL')
            resuming = False
        if resuming:
            base = self._resume_url or ''
            url = base + ('&' if '?' in base else '?') + _GATEWAY_QUERY
        else:
            self._reset_session()
            gate = self._identify_gate(token)
            if gate is None:
                return
            url = gate
        self._active = cfg
        if stop.is_set():
            return
        ws = self._connect(url)
        self._ws = ws
        self._acked = True
        self._next_beat = float('inf')
        status: Optional[int] = None
        try:
            while not stop.is_set():
                if self._clock() >= self._next_beat:
                    if not self._acked:
                        # No ACK since the last beat: the connection is a zombie.
                        logger.warning('Discord gateway: heartbeat not acknowledged, reconnecting')
                        status = RESUMABLE_CLOSE_CODE
                        return
                    self._heartbeat(ws)
                    self._next_beat = self._clock() + self._interval
                ws.resumable = bool(self._session_id) and not stop.is_set()
                try:
                    opcode, frame = ws.recv_data_frame(True)
                except MessageTooLarge:
                    self._too_large(cfg)
                    status = OVERSIZE_CLOSE_CODE
                    return
                except Exception as exc:
                    if websocket is not None and isinstance(exc, websocket.WebSocketTimeoutException):
                        continue
                    logger.info('Discord gateway closed: %s', type(exc).__name__)
                    return
                if stop.is_set():
                    return  # cancelled while blocked: act on nothing that arrived
                if opcode == OPCODE_CLOSE:
                    self._on_close(frame)
                    return
                if opcode != OPCODE_TEXT:
                    continue
                data = getattr(frame, 'data', frame)
                if isinstance(data, str):
                    data = data.encode('utf-8')
                if len(data) > cfg.max_event_bytes:  # the reader's own check, belt and braces
                    self._too_large(cfg)
                    status = OVERSIZE_CLOSE_CODE
                    return
                if not self._on_message(ws, data):
                    return
        finally:
            self._ws = None
            if status is None:
                keep = bool(self._session_id) and not stop.is_set()
                status = RESUMABLE_CLOSE_CODE if keep else ORDERLY_CLOSE_CODE
            try:
                ws.close(status=status)
            except Exception as exc:
                logger.debug('Discord gateway close failed: %s', type(exc).__name__)

    def _reset_session(self) -> None:
        self._session_id = None
        self._resume_url = None
        self._seq = None
        self._owners.clear()  # rebuilt from the new session's GUILD_CREATE stream

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

    def _on_message(self, ws: Any, data: bytes) -> bool:
        """Handle one complete, size-checked gateway message. False ends this connection."""
        try:
            msg = json.loads(data.decode('utf-8'))
        except (ValueError, TypeError, RecursionError):
            logger.warning('Discord gateway sent a frame that is not usable JSON')
            return True
        if not isinstance(msg, dict):
            return True
        op = msg.get('op')
        seq = msg.get('s')
        if isinstance(seq, int):
            self._seq = seq
        if op == OP_HELLO:
            body = msg.get('d') or {}
            try:
                interval = float(body.get('heartbeat_interval', 41250)) / 1000.0
            except (TypeError, ValueError):
                interval = 41.25
            self._interval = interval if math.isfinite(interval) and interval > 0 else 41.25
            self._next_beat = self._clock() + self._interval * self._jitter()
            return self._identify_or_resume(ws)
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

    def _identify_or_resume(self, ws: Any) -> bool:
        """Send Resume or Identify; False when cancelled (nothing is sent then).

        At HELLO a session id exists only if _session decided to resume."""
        if self._stop_event.is_set():
            return False
        token = self._active.token
        if self._session_id and self._seq is not None:
            self._send_frame(ws, {'op': OP_RESUME, 'd': {
                'token': token, 'session_id': self._session_id, 'seq': self._seq}})
            return True
        self._identify_times.append(self._clock())
        self._send_frame(ws, {'op': OP_IDENTIFY, 'd': {
            'token': token,
            'intents': INTENTS,
            'properties': {'os': sys.platform, 'browser': 'hypeman', 'device': 'hypeman'},
        }})
        return True

    def _dispatch(self, kind: Optional[str], body: Any) -> None:
        if self._stop_event.is_set() or not isinstance(body, dict):
            return
        if kind == 'READY':
            self._session_id = str(body.get('session_id') or '') or None
            resume = body.get('resume_gateway_url')
            if is_trusted_gateway_url(resume):
                self._resume_url = resume
            else:
                self._resume_url = None
                logger.error('Discord gateway: READY carried an untrusted resume URL; ignoring it')
            user = body.get('user') or {}
            self._bot_id = str(user.get('id')) if user.get('id') is not None else None
            self._owners.clear()
            self._ready_at = self._clock()
            logger.info('Discord gateway ready')
        elif kind == 'RESUMED':
            self._ready_at = self._clock()
            logger.info('Discord gateway resumed')
        elif kind in ('GUILD_CREATE', 'GUILD_UPDATE'):
            guild = body.get('id')
            if guild is None:
                return
            owner = body.get('owner_id')
            if owner is not None and not body.get('unavailable'):
                self._owners[str(guild)] = str(owner)
            else:
                self._owners.pop(str(guild), None)  # unknown owner: fail closed
        elif kind == 'GUILD_DELETE':
            if body.get('id') is not None:
                self._owners.pop(str(body['id']), None)
        elif kind == 'MESSAGE_CREATE':
            event = self.to_event(body)
            handler = self._handler
            if event is not None and handler is not None:
                try:
                    handler(event)
                except BaseException as exc:
                    # Foreign code. Class name only, and nothing escapes to kill the
                    # worker or reach the thread excepthook with chained context.
                    logger.warning('Discord chat handler failed: %s', self._describe(exc))

    # -- conversion --------------------------------------------------------

    def to_event(self, msg: Dict[str, Any]) -> Optional[ChatEvent]:
        """Convert a MESSAGE_CREATE body, or None when it is not ours to hear."""
        cfg = self._cfg  # one snapshot for the whole decision
        if cfg.error:
            return None  # a supplied setting we could not read: hear nothing
        guild_id = msg.get('guild_id')
        if guild_id is None:
            return None  # a direct message: never read
        guild_id = str(guild_id)
        channel_id = str(msg.get('channel_id', ''))
        if cfg.guild_id and guild_id != cfg.guild_id:
            return None
        if cfg.channel_ids and channel_id not in cfg.channel_ids:
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
            if cfg.mod_role_ids & {str(r) for r in (member.get('roles') or [])}:
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
        except BaseException as exc:  # the documented contract: never raises
            logger.warning('Discord send failed: %s', self._describe(exc))
            return False

    def _admit(self, channel: str, started: float) -> bool:
        """Wait until the global and this channel's deadlines have both passed.

        The deadlines are re-read after every wait, because another caller may
        have extended them meanwhile. False when waiting would pass the cap,
        measured on the monotonic clock from ``started``."""
        while True:
            with self._rate_lock:
                pause = max(self._global_until, self._rate_until.get(channel, 0.0)) - self._clock()
            if pause <= 0:
                return True
            if self._clock() - started + pause > MAX_RATE_WAIT:
                return False
            self._sleep(pause)

    def _send(self, channel: str, text: str, reply_to_id: Optional[str]) -> bool:
        token = self._cfg.token
        if not token or not _is_snowflake(str(channel)) or not text:
            return False
        if len(text) > MESSAGE_LIMIT:
            text = text[:MESSAGE_LIMIT - 1] + '…'
        body: Dict[str, Any] = {
            'content': text,
            # Echoed user text must never ping @everyone, a role, or a user.
            'allowed_mentions': {'parse': []},
            # Create Message deduplication (documented): with enforce_nonce, a
            # repeat of this nonce from the same author returns the first
            # message instead of creating another, so a retry after a 5xx whose
            # outcome we could not see cannot post twice.
            'nonce': secrets.token_hex(8),
            'enforce_nonce': True,
        }
        if reply_to_id and _is_snowflake(str(reply_to_id)):
            body['message_reference'] = {
                'message_id': str(reply_to_id), 'fail_if_not_exists': False}
        url = f'{API_BASE}/channels/{channel}/messages'
        headers = {'Authorization': f'Bot {token}', 'User-Agent': USER_AGENT}
        # One request per channel at a time: the bucket state a response reports
        # is recorded before the next caller on that channel is admitted. A fixed
        # stripe, chosen by the id, so the lock for a channel never changes
        # identity (two channels may share a stripe and take turns; that is safe).
        with self._channel_locks[int(channel) % LOCK_STRIPES]:
            return self._post(channel, url, body, headers)

    def _post(self, channel: str, url: str, body: Dict[str, Any], headers: Dict[str, str]) -> bool:
        started = self._clock()
        for attempt in range(MAX_SEND_ATTEMPTS):
            if not self._admit(channel, started):
                logger.warning('Discord send: rate limited for longer than the cap')
                return False
            resp = self._session_http().post(
                url, json=body, headers=headers, timeout=REQUEST_TIMEOUT,
                allow_redirects=False)
            status = resp.status_code
            if status in (200, 201):
                self._note_bucket(channel, resp)
                return True
            if status == 429:
                delay = self._retry_after(resp)
                if delay is None:
                    logger.warning('Discord send: rate limit with an unusable retry delay')
                    return False
                self._note_429(channel, resp, delay)
                if self._clock() - started + delay > MAX_RATE_WAIT or attempt == MAX_SEND_ATTEMPTS - 1:
                    logger.warning('Discord send: rate limited, giving up')
                    return False
                continue  # the next admission waits out the recorded deadline
            if status >= 500 and attempt < MAX_SEND_ATTEMPTS - 1:
                self._sleep(min(2.0 ** attempt, 4.0))
                continue
            logger.warning('Discord send rejected with HTTP %s', status)
            return False
        return False

    def _note_429(self, channel: str, resp: Any, delay: float) -> None:
        """Record a 429's deadline where every other caller will see it."""
        headers = resp.headers or {}
        is_global = (str(headers.get('X-RateLimit-Global', '')).lower() == 'true'
                     or str(headers.get('X-RateLimit-Scope', '')).lower() == 'global')
        try:
            is_global = is_global or resp.json().get('global') is True
        except Exception:
            pass
        deadline = self._clock() + min(delay, MAX_BUCKET_WAIT)
        with self._rate_lock:
            if is_global:
                self._global_until = max(self._global_until, deadline)
            else:
                self._rate_until[channel] = max(self._rate_until.get(channel, 0.0), deadline)

    def _note_bucket(self, channel: str, resp: Any) -> None:
        """Remember an exhausted bucket so the next send waits instead of eating a 429."""
        headers = resp.headers or {}
        try:
            if str(headers.get('X-RateLimit-Remaining')) == '0':
                after = float(headers.get('X-RateLimit-Reset-After', 0))
                if not math.isfinite(after):
                    return
                now = self._clock()
                with self._rate_lock:
                    self._rate_until[channel] = now + min(max(0.0, after), MAX_BUCKET_WAIT)
                    if len(self._rate_until) > 256:
                        self._rate_until = {k: v for k, v in self._rate_until.items() if v > now}
        except (TypeError, ValueError):
            return

    @staticmethod
    def _retry_after(resp: Any) -> Optional[float]:
        """The delay a 429 asks for; None when it is not a finite number."""
        value: Any = None
        try:
            value = resp.json().get('retry_after')
        except Exception:
            value = None
        if value is None:
            value = (resp.headers or {}).get('Retry-After')
        if value is None:
            return 1.0
        try:
            delay = float(value)
        except (TypeError, ValueError):
            return 1.0
        if not math.isfinite(delay):
            return None
        return max(0.0, delay)


__all__ = ['DiscordChat', 'INTENTS', 'MESSAGE_LIMIT', 'is_trusted_gateway_url']
