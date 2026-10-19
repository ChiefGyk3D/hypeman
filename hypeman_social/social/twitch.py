# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Twitch chat: hear a channel over EventSub WebSocket, answer through Helix.

The bot is its own Twitch account with its own Twitch application, never the
broadcaster's account. It listens with ``channel.chat.message`` and speaks with
``Send Chat Message``. This adapter converts and delivers; who the bot is, when
it speaks and what it may say belong to the consumer, not to this file.

Security notes that shaped the code:

* Credentials come only from ``platform_secret('Twitch', key)``. No token,
  client secret or ``Authorization`` header is ever logged or placed in an
  exception message; failures are logged by exception *type* and HTTP status.
  Sanitised private exceptions are raised after leaving the ``except`` block so
  they carry no ``__context__`` holding the original, secret-bearing one.
* Twitch requires the token to be validated at start and hourly. A token that
  cannot be validated or refreshed marks the adapter unavailable with one clear
  error, then a single cooldown-guarded retry per cooldown, never a storm.
* A replacement token is staged and only published to senders as an immutable
  snapshot once it has been validated; the snapshot carries the absolute expiry
  Twitch reported, and nothing is trusted past it.
* The bot only ever speaks in the one channel it was configured for, and only
  hears messages that originate in that channel (shared chat is dropped).
* A refreshed token is held in memory only, plus an optional callback so the
  consumer can persist it. There is deliberately no Doppler write path here (it
  would need a write-scoped Doppler token); see docs/TWITCH_BOT.md.
"""

import collections
import json
import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from hypeman_social.config.secrets import reset_secret_cache
from hypeman_social.social.base import platform_secret
from hypeman_social.social.events import (
    ROLE_BROADCASTER,
    ROLE_MODERATOR,
    ROLE_SUBSCRIBER,
    ROLE_VIP,
    ChatEvent,
    ChatHandler,
    ChatReceiver,
)

try:
    import websocket  # websocket-client
except ImportError:  # extras contract: importable on a bare install
    # Same pattern as bluesky.py: the module name is rebound to None when absent.
    websocket = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

EVENTSUB_URL = 'wss://eventsub.wss.twitch.tv/ws'
#: The only host a reconnect URL may name. The docs show reconnect URLs as
#: ``wss://eventsub.wss.twitch.tv?...`` (no path) and the connect URL with
#: ``/ws``; those two path forms are the only ones accepted.
EVENTSUB_HOST = 'eventsub.wss.twitch.tv'
EVENTSUB_PATHS = ('', '/', '/ws')
VALIDATE_URL = 'https://id.twitch.tv/oauth2/validate'
TOKEN_URL = 'https://id.twitch.tv/oauth2/token'
HELIX = 'https://api.twitch.tv/helix'

#: The documented valid range is 10 to 600; 30 keeps dead links short.
KEEPALIVE_SECONDS = 30
KEEPALIVE_MIN = 10
KEEPALIVE_MAX = 600
#: Slack on top of the keepalive window before a silent link is declared dead.
KEEPALIVE_GRACE = 5
#: Twitch closes a connection that has not subscribed within 10 seconds.
WELCOME_TIMEOUT = 10
#: How long stop() waits for the worker before reporting an incomplete shutdown.
STOP_TIMEOUT = 5.0

#: Twitch requires validation at start and hourly; re-check a little early.
VALIDATE_INTERVAL = 3300
#: Refresh (and revalidate) when less than this is left before the known expiry.
REFRESH_MARGIN = 900
#: After an auth failure, wait this long before one more attempt.
AUTH_COOLDOWN = 900
#: Minimum gap between attempts that failed for a transient reason.
ATTEMPT_COOLDOWN = 30
HTTP_TIMEOUT = 10

BACKOFF_BASE = 1.0
BACKOFF_MAX = 60.0
#: Consecutive failed connection cycles before a long rest.
MAX_CONSECUTIVE_FAILURES = 10
LONG_REST = 300.0

#: Documented limit of Send Chat Message.
MAX_MESSAGE_CHARS = 500
#: Documented regular-account limits are 20 per 30 s and 1 per second per
#: channel for a sender who is not broadcaster, moderator or VIP. Stay below.
SEND_WINDOW_SECONDS = 30.0
SEND_WINDOW_MAX = 15
SEND_MIN_INTERVAL = 1.5
#: Chat messages that begin with one of these are commands to Twitch chat.
COMMAND_PREFIXES = ('/', '.')

#: Upper bound on one frame and on one fragmented message, in encoded bytes.
MAX_FRAME_BYTES = 1024 * 1024
#: Twitch's own guidance: ignore a notification whose ``message_timestamp`` is
#: older than ten minutes (dev.twitch.tv/docs/eventsub, replay attacks).
REPLAY_WINDOW = 600.0
#: Size bound on the seen-id cache, next to the REPLAY_WINDOW time bound.
SEEN_IDS = 4096
#: After a message from another channel of a shared chat session is seen, the
#: bot stays quiet this long: a user-token send would reach every channel.
SHARED_CHAT_QUIET = 600.0

REQUIRED_SCOPES = ('user:read:chat', 'user:write:chat')

_ROLE_BADGES = {
    'broadcaster': ROLE_BROADCASTER,
    'moderator': ROLE_MODERATOR,
    'subscriber': ROLE_SUBSCRIBER,
    'vip': ROLE_VIP,
}

_TIMESTAMP = re.compile(
    r'^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?'
    r'(Z|[+-]\d{2}:\d{2})$')


class _AuthLost(Exception):
    """The token can no longer be used; end the session."""


class _Transient(Exception):
    """A failure worth another try after backoff."""


class FrameTooLarge(Exception):
    """A websocket frame or message exceeded MAX_FRAME_BYTES."""


@dataclass(frozen=True)
class _Validated:
    """Immutable view of an access token that passed validation.

    Senders read only this; the refresh token and any unvalidated replacement
    never reach them.
    """

    access_token: str
    client_id: str
    bot_user_id: str
    validated_at: float
    #: Absolute deadline on the adapter clock, None when Twitch reported none.
    expires_at: Optional[float]


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError) or type(exc).__name__ == 'WebSocketTimeoutException'


def _make_bounded_websocket() -> Any:
    """A websocket-client WebSocket that refuses oversize frames up front.

    The stock reader trusts the length in a frame header and allocates for it
    (and for every fragment of a message) before any size check can run. The
    subclass checks the declared length straight after the header is read, so a
    frame or an aggregate over MAX_FRAME_BYTES is refused before its payload is
    received. websocket-client has no per-message limit option of its own, so
    this leans on its ``frame_buffer`` internals; if they change shape the
    function returns None and the caller falls back to the stock class plus the
    after-the-fact byte check in ``_recv`` (see docs/TWITCH_BOT.md).
    """
    if websocket is None:
        return None
    try:
        from websocket._abnf import ABNF, frame_buffer
    except ImportError:
        return None

    class _BoundedFrameBuffer(frame_buffer):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._message_bytes = 0

        def recv_length(self) -> None:
            super().recv_length()
            header: Any = self.header
            opcode = header[4]  # (fin, rsv1, rsv2, rsv3, opcode, mask, length)
            earlier = self._message_bytes if opcode == ABNF.OPCODE_CONT else 0
            if (self.length or 0) + earlier > MAX_FRAME_BYTES:
                raise FrameTooLarge('frame over the size limit')

        def recv_frame(self) -> Any:
            frame = super().recv_frame()
            if frame.opcode in (ABNF.OPCODE_TEXT, ABNF.OPCODE_BINARY):
                self._message_bytes = 0 if frame.fin else len(frame.data)
            elif frame.opcode == ABNF.OPCODE_CONT:
                self._message_bytes = 0 if frame.fin else self._message_bytes + len(frame.data)
            return frame

    class _BoundedWebSocket(websocket.WebSocket):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.frame_buffer = _BoundedFrameBuffer(
                self._recv, bool(kwargs.get('skip_utf8_validation', False)))

    return _BoundedWebSocket


_BoundedWebSocket = _make_bounded_websocket()


def _default_connect(url: str, timeout: float) -> Any:
    # redirect_limit=0: websocket-client would otherwise follow a 3xx from the
    # handshake to any host, past every URL check made before connecting.
    options: Dict[str, Any] = {'timeout': timeout, 'redirect_limit': 0}
    if _BoundedWebSocket is not None:
        options['class_'] = _BoundedWebSocket
    ws = websocket.create_connection(url, **options)
    # websocket-client 1.9.0 marks a socket connected after a 3xx handshake
    # reply when redirect_limit is 0 (1.9.2 raises); accept only a real upgrade.
    if ws.getstatus() != 101:
        ws.close()
        raise OSError('EventSub handshake was not an upgrade')
    return ws


def _parse_timestamp(value: Any) -> Optional[float]:
    """RFC 3339 (nanosecond fractions allowed) to epoch seconds, None if unusable."""
    if not isinstance(value, str):
        return None
    match = _TIMESTAMP.match(value)
    if not match:
        return None
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    try:
        moment = datetime(int(year), int(month), int(day), int(hour), int(minute),
                          int(second), tzinfo=timezone.utc)
        if zone != 'Z':
            offset = timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6]))
            moment = moment - offset if zone[0] == '+' else moment + offset
        return moment.timestamp() + (float('0.' + fraction) if fraction else 0.0)
    except (ValueError, OverflowError):
        return None


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def event_to_chat_event(event: Dict[str, Any], bot_user_id: str) -> Optional[ChatEvent]:
    """Convert a ``channel.chat.message`` event; None when it is malformed."""
    try:
        chatter_id = str(event['chatter_user_id'])
        message_id = str(event['message_id'])
        text = str((event.get('message') or {}).get('text') or '')
    except (KeyError, TypeError, AttributeError):
        return None
    if not chatter_id or not message_id:
        return None

    roles = set()
    for badge in event.get('badges') or []:
        if isinstance(badge, dict) and badge.get('set_id') in _ROLE_BADGES:
            roles.add(_ROLE_BADGES[badge['set_id']])
    # The channel owner is the broadcaster whatever the badge list says.
    if chatter_id == str(event.get('broadcaster_user_id')):
        roles.add(ROLE_BROADCASTER)

    reply = event.get('reply')
    reply_to = None
    if isinstance(reply, dict) and reply.get('parent_message_id'):
        reply_to = str(reply['parent_message_id'])

    extra = {'chatter_login': str(event.get('chatter_user_login') or ''),
             'broadcaster_user_id': str(event.get('broadcaster_user_id') or ''),
             'message_type': str(event.get('message_type') or '')}
    return ChatEvent(
        platform='twitch',
        channel=str(event.get('broadcaster_user_login') or event.get('broadcaster_user_id') or ''),
        message_id=message_id,
        author_id=chatter_id,
        author_name=str(event.get('chatter_user_name') or event.get('chatter_user_login') or ''),
        text=text,
        is_self=chatter_id == str(bot_user_id),
        bridged=False,
        roles=frozenset(roles),
        reply_to_id=reply_to,
        extra=extra,
    )


def clip_message(text: str) -> str:
    """One clean line, at most MAX_MESSAGE_CHARS, cut at a word when that is cheap.

    Whitespace of every kind (including the C0/C1 whitespace controls) collapses
    to single spaces; every other control character is removed.
    """
    kept = []
    for ch in str(text):
        if ch.isspace():
            kept.append(' ')
        elif unicodedata.category(ch) != 'Cc':
            kept.append(ch)
    text = ' '.join(''.join(kept).split())
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    cut = text[:MAX_MESSAGE_CHARS - 1]
    space = cut.rfind(' ')
    if space >= MAX_MESSAGE_CHARS - 60:
        cut = cut[:space]
    return cut.rstrip() + '…'


def has_command_prefix(message: str) -> bool:
    """True when chat would treat ``message`` as a command (``/ban``, ``.ban``).

    Invisible format characters (zero-width space, bidi marks) in front of the
    prefix are skipped, so they cannot be used to hide it.
    """
    for ch in message:
        if ch.isspace() or unicodedata.category(ch) == 'Cf':
            continue
        return ch in COMMAND_PREFIXES
    return False


class TwitchChat(ChatReceiver):
    """Twitch chat for one channel, as a dedicated bot account."""

    name = 'twitch'

    def __init__(
        self,
        http: Any = None,
        connect: Optional[Callable[[str, float], Any]] = None,
        clock: Callable[[], float] = time.monotonic,
        backoff_base: float = BACKOFF_BASE,
        long_rest: float = LONG_REST,
        auth_poll: float = ATTEMPT_COOLDOWN,
        on_token_refresh: Optional[Callable[[str, str], None]] = None,
        allow_command_prefix: bool = False,
        stop_timeout: float = STOP_TIMEOUT,
    ):
        """
        ``on_token_refresh(access_token, refresh_token)`` is called after every
        successful refresh so the consumer can persist the replacement securely;
        exceptions from it are swallowed (logged by type only). The values are
        never logged here. ``allow_command_prefix`` lets ``send()`` submit text
        that starts with ``/`` or ``.``; the default refuses it.
        """
        self._http = http if http is not None else requests.Session()
        self._connect = connect or _default_connect
        self._clock = clock
        self._backoff_base = backoff_base
        self._long_rest = long_rest
        self._auth_poll = auth_poll
        self._keepalive = KEEPALIVE_SECONDS
        self._on_token_refresh = on_token_refresh
        self._allow_command_prefix = allow_command_prefix
        self._stop_timeout = stop_timeout

        self._lock = threading.RLock()
        self._creds: Dict[str, str] = {}
        # Newest token pair held in memory. The access token here is a
        # candidate: senders only ever see ``_snapshot``.
        self._mem_access: Optional[str] = None
        self._mem_refresh: Optional[str] = None
        # The pair last read from the secret store, to tell a changed store
        # (the operator replaced credentials) from an unchanged, older one.
        self._loaded_pair: Optional[Tuple[str, str]] = None
        self._snapshot: Optional[_Validated] = None
        self._pending_refresh: Optional[Tuple[str, str]] = None
        self._callback_lock = threading.Lock()
        self._last_attempt: Optional[float] = None
        self._auth_failed_at: Optional[float] = None
        self._auth_error_logged = False
        self._memory_only_noted = False
        self._missing_logged = False
        self._broadcaster_id: Optional[str] = None
        self._broadcaster_login: Optional[str] = None

        self._handler: Optional[ChatHandler] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._ws: Any = None
        self._seen: 'collections.OrderedDict[str, float]' = collections.OrderedDict()
        self._stale_logged = False
        self._shared_chat_until = 0.0
        self._sent_times: Deque[float] = collections.deque()
        self._send_blocked_until = 0.0
        self._last_thread_check = 0.0

    def __repr__(self) -> str:
        return '<TwitchChat>'

    # ----- credentials and token health ---------------------------------

    def _load_credentials(self) -> bool:
        keys = ('client_id', 'client_secret', 'access_token', 'refresh_token',
                'bot_user_id', 'channel')
        creds = {k: (platform_secret('Twitch', k) or '').strip() for k in keys}
        stored = (creds['access_token'], creds['refresh_token'])
        with self._lock:
            self._creds = creds
            # Adopt the stored pair on first read, or when it differs from the
            # one read last time (the operator replaced it). An unchanged,
            # older stored pair never overwrites a newer pair rotated in memory.
            if self._loaded_pair is None or stored != self._loaded_pair:
                self._mem_access = stored[0] or None
                self._mem_refresh = stored[1] or None
            self._loaded_pair = stored
        missing = [k for k in ('client_id', 'access_token', 'bot_user_id', 'channel')
                   if not creds[k]]
        if missing:
            if not self._missing_logged:
                self._missing_logged = True
                logger.error('Twitch chat is not configured; missing: %s', ', '.join(missing))
            return False
        self._missing_logged = False
        return True

    def _auth_snapshot(self) -> Optional[_Validated]:
        """The validated credentials, if still inside their known lifetime."""
        with self._lock:
            snap = self._snapshot
            if snap is not None and self._usable(snap, self._clock()):
                return snap
            return None

    @staticmethod
    def _auth_headers(snap: _Validated) -> Dict[str, str]:
        return {'Authorization': f'Bearer {snap.access_token}', 'Client-Id': snap.client_id}

    def _fail_auth(self, reason: str) -> None:
        self._auth_failed_at = self._clock()
        self._snapshot = None
        if not self._auth_error_logged:
            self._auth_error_logged = True
            logger.error('Twitch chat disabled: %s. Fix the credentials; one retry is made '
                         'every %d minutes.', reason, AUTH_COOLDOWN // 60)

    def _request(self, method: str, url: str, **kwargs: Any) -> Optional[Any]:
        """HTTP call that never raises and never follows redirects."""
        try:
            fn = getattr(self._http, method)
            return fn(url, timeout=HTTP_TIMEOUT, allow_redirects=False, **kwargs)
        except Exception as e:
            logger.warning('Twitch %s request failed: %s', method.upper(), type(e).__name__)
            return None

    def _validate(self, token: Optional[str]) -> Tuple[str, int]:
        """('ok'|'invalid'|'bad'|'transient', expires_in). 'bad' = wrong account."""
        resp = self._request('get', VALIDATE_URL, headers={'Authorization': f'OAuth {token}'})
        if resp is None:
            return 'transient', 0
        if resp.status_code == 401:
            return 'invalid', 0
        if resp.status_code != 200:
            logger.warning('Twitch auth validation returned HTTP %s', resp.status_code)
            return 'transient', 0
        try:
            body = resp.json()
        except Exception:
            return 'transient', 0
        if body.get('client_id') != self._creds['client_id']:
            self._fail_auth('the access token belongs to a different Twitch application')
            return 'bad', 0
        if str(body.get('user_id')) != self._creds['bot_user_id']:
            self._fail_auth('the access token belongs to a different Twitch user than bot_user_id')
            return 'bad', 0
        missing = [s for s in REQUIRED_SCOPES if s not in (body.get('scopes') or [])]
        if missing:
            self._fail_auth('the access token lacks scope(s): ' + ', '.join(missing))
            return 'bad', 0
        return 'ok', max(_as_int(body.get('expires_in'), 0), 0)

    def _refresh(self) -> bool:
        """Swap in a refreshed pair as the in-memory candidate (not yet published)."""
        if not (self._mem_refresh and self._creds.get('client_secret')):
            return False
        resp = self._request('post', TOKEN_URL, data={
            'grant_type': 'refresh_token',
            'refresh_token': self._mem_refresh,
            'client_id': self._creds['client_id'],
            'client_secret': self._creds['client_secret'],
        })
        if resp is None or resp.status_code != 200:
            if resp is not None:
                logger.warning('Twitch auth refresh returned HTTP %s', resp.status_code)
            return False
        try:
            body = resp.json()
            new_access = body['access_token']
            # Twitch says a refresh token MAY change; keep the old one if not.
            new_refresh = body.get('refresh_token') or self._mem_refresh
            if not (isinstance(new_access, str) and new_access
                    and isinstance(new_refresh, str)):
                return False
        except Exception:
            return False
        self._mem_access = new_access
        self._mem_refresh = new_refresh
        with self._callback_lock:
            self._pending_refresh = (new_access, new_refresh)
        if self._on_token_refresh is None and not self._memory_only_noted:
            self._memory_only_noted = True
            logger.warning('Twitch tokens were refreshed and are held in memory only; the '
                           'stored copies are not updated (see docs/TWITCH_BOT.md).')
        return True

    def _flush_refresh_callback(self) -> None:
        """Hand a fresh pair to on_token_refresh, outside the adapter lock."""
        with self._callback_lock:
            pending, self._pending_refresh = self._pending_refresh, None
        if pending is None or self._on_token_refresh is None:
            return
        try:
            self._on_token_refresh(pending[0], pending[1])
        except Exception as e:
            logger.warning('Twitch refresh callback failed: %s', type(e).__name__)

    def _ensure_auth(self, force: bool = False) -> bool:
        """True when the token is validated and fresh; heals within cooldowns."""
        try:
            with self._lock:
                return self._ensure_auth_locked(force)
        finally:
            self._flush_refresh_callback()

    def _ensure_auth_locked(self, force: bool) -> bool:
        now = self._clock()
        snap = self._snapshot
        recovering = False
        if self._auth_failed_at is not None:
            if now - self._auth_failed_at < AUTH_COOLDOWN:
                return False
            recovering = True
        elif not force and snap is not None and self._fresh(snap, now):
            return True
        elif (not force and self._last_attempt is not None
              and now - self._last_attempt < ATTEMPT_COOLDOWN):
            return self._auth_snapshot() is not None
        self._last_attempt = now

        if recovering:
            # The secret layer caches the whole Doppler project for the life of
            # the process, so without this a corrected secret is never seen.
            try:
                reset_secret_cache()
            except Exception as e:
                logger.debug('Twitch cache reset failed: %s', type(e).__name__)
        if not self._load_credentials():
            return False

        verdict, expires = self._validate(self._mem_access)
        if verdict == 'invalid':
            self._snapshot = None  # Twitch says this token is dead
        if verdict == 'invalid' or (verdict == 'ok' and 0 < expires < REFRESH_MARGIN):
            if self._refresh():
                verdict, expires = self._validate(self._mem_access)
                if verdict == 'invalid':
                    self._snapshot = None
            elif verdict == 'invalid':
                self._fail_auth('the access token is invalid and could not be refreshed')
                return False
        if verdict == 'ok':
            done = self._clock()
            self._snapshot = _Validated(
                access_token=self._mem_access or '',
                client_id=self._creds['client_id'],
                bot_user_id=self._creds['bot_user_id'],
                validated_at=done,
                expires_at=done + expires if expires else None)
            self._auth_failed_at = None
            self._auth_error_logged = False
            return True
        if verdict == 'invalid':
            self._fail_auth('the access token is invalid after a refresh')
            return False
        if verdict == 'bad':
            return False
        return self._auth_snapshot() is not None

    @staticmethod
    def _fresh(snap: _Validated, now: float) -> bool:
        """Validated recently and not close to its known expiry."""
        if now - snap.validated_at >= VALIDATE_INTERVAL:
            return False
        return snap.expires_at is None or now < snap.expires_at - REFRESH_MARGIN

    @staticmethod
    def _usable(snap: _Validated, now: float) -> bool:
        """A transient failure does not kill a token that just worked, but never
        past the stale limit and never past the expiry Twitch reported."""
        if now - snap.validated_at >= 2 * VALIDATE_INTERVAL:
            return False
        return snap.expires_at is None or now < snap.expires_at

    def _resolve_channel(self) -> bool:
        if self._broadcaster_id:
            return True
        channel = self._creds.get('channel', '')
        if channel.isdigit():
            self._broadcaster_id = channel
            return True
        snap = self._auth_snapshot()
        if snap is None:
            return False
        resp = self._request('get', f'{HELIX}/users', headers=self._auth_headers(snap),
                             params={'login': channel.lower()})
        try:
            if resp is not None and resp.status_code == 200:
                data = resp.json().get('data') or []
                if data:
                    self._broadcaster_id = str(data[0]['id'])
                    self._broadcaster_login = str(data[0].get('login') or channel.lower())
                    return True
        except Exception as e:
            logger.warning('Twitch channel lookup failed: %s', type(e).__name__)
        logger.warning('Twitch channel could not be resolved to a user id')
        return False

    # ----- ChatReceiver ---------------------------------------------------

    def is_available(self) -> bool:
        try:
            if websocket is None:
                logger.error('Twitch chat needs the "twitch" extra: pip install '
                             '"hypeman-social[twitch]"')
                return False
            ok = self._ensure_auth()
            self._heal_thread()
            return ok
        except Exception as e:
            logger.warning('Twitch availability check failed: %s', type(e).__name__)
            return False

    def _heal_thread(self) -> None:
        """Restart a receive thread that died, at most once per cooldown."""
        with self._lock:
            if self._handler is None or self._stop.is_set():
                return
            if self._thread is not None and self._thread.is_alive():
                return
            now = self._clock()
            if now - self._last_thread_check < ATTEMPT_COOLDOWN and self._last_thread_check:
                return
            self._last_thread_check = now
            self._spawn()

    def start(self, handler: ChatHandler) -> None:
        if websocket is None:
            logger.error('Twitch chat needs the "twitch" extra: pip install '
                         '"hypeman-social[twitch]"')
            return
        with self._lock:
            self._handler = handler
            if self._thread is not None and self._thread.is_alive():
                if self._stop.is_set():
                    logger.warning('Twitch chat start refused: the previous receiver has not '
                                   'finished stopping')
                return
            self._stop.clear()
            self._spawn()

    def _spawn(self) -> None:
        self._thread = threading.Thread(target=self._serve, name='twitch-chat', daemon=True)
        self._thread.start()

    # Reason for the override ignore below: ChatReceiver.stop returns None; a bool
    # is a harmless widening for callers and reports an incomplete stop.
    def stop(self) -> bool:  # type: ignore[override]
        """Stop the receiver. False when the worker is still alive afterwards.

        A worker stuck in a blocking connect cannot be interrupted from here. It
        is kept (not forgotten) so ``start()`` can refuse to run a second one;
        it exits by itself as soon as the connect returns.
        """
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception as e:
                logger.debug('Twitch websocket close failed: %s', type(e).__name__)
        thread = self._thread
        if thread is not None:
            if thread is threading.current_thread():
                logger.debug('Twitch stop() called from the receiver thread; it ends when '
                             'the handler returns')
                return False
            thread.join(timeout=self._stop_timeout)
            if thread.is_alive():
                logger.warning('Twitch chat shutdown incomplete: the receiver is still '
                               'running (blocked connect?); start() is refused until it ends')
                return False
        with self._lock:
            if self._thread is thread:
                self._thread = None
        self._ws = None
        return True

    # ----- receive loop ---------------------------------------------------

    def _serve(self) -> None:
        failures = 0
        while not self._stop.is_set():
            if not self._ensure_auth():
                self._stop.wait(self._auth_poll)
                continue
            try:
                subscribed = self._cycle()
            except Exception as e:
                logger.warning('Twitch chat connection ended: %s', type(e).__name__)
                subscribed = False
            if self._stop.is_set():
                break
            failures = 0 if subscribed else failures + 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                logger.error('Twitch chat failed %d times in a row; resting %ds',
                             failures, int(self._long_rest))
                failures = 0
                self._stop.wait(self._long_rest)
            else:
                delay = min(self._backoff_base * (2 ** max(failures - 1, 0)), BACKOFF_MAX)
                self._stop.wait(delay)

    def _recv(self, ws: Any, timeout: float) -> Optional[Dict[str, Any]]:
        """One JSON frame, None on timeout; raises on closed or garbage links."""
        try:
            ws.settimeout(timeout)
        except Exception as e:
            logger.debug('Twitch websocket settimeout failed: %s', type(e).__name__)
        # Classify inside the handler, raise after leaving it: raising from
        # within would keep the original (possibly secret-bearing) exception as
        # __context__ even with ``from None``.
        failure = ''
        raw: Any = None
        try:
            raw = ws.recv()
        except Exception as e:
            if isinstance(e, FrameTooLarge):
                failure = 'oversize'
            elif _is_timeout(e):
                failure = 'timeout'
            else:
                failure = 'closed'
        if failure == 'timeout':
            return None
        if failure == 'oversize':
            logger.warning('Twitch frame over %d bytes refused; reconnecting', MAX_FRAME_BYTES)
            raise _Transient('oversize frame')
        if failure:
            raise _Transient('websocket closed')
        if not raw:
            raise _Transient('websocket closed')
        size = len(raw) if isinstance(raw, (bytes, bytearray)) else len(str(raw).encode('utf-8'))
        if size > MAX_FRAME_BYTES:
            logger.warning('Twitch frame over %d bytes dropped', MAX_FRAME_BYTES)
            return {}
        try:
            frame = json.loads(raw)
        except (ValueError, RecursionError):
            return {}
        return frame if isinstance(frame, dict) else {}

    def _welcome(self, ws: Any) -> Tuple[str, int, Optional[str]]:
        frame = self._recv(ws, WELCOME_TIMEOUT)
        if not frame or (frame.get('metadata') or {}).get('message_type') != 'session_welcome':
            raise _Transient('no welcome')
        session = (frame.get('payload') or {}).get('session') or {}
        sid = str(session.get('id') or '')
        if not sid:
            raise _Transient('welcome without session id')
        keepalive = _as_int(session.get('keepalive_timeout_seconds'), KEEPALIVE_SECONDS)
        if not KEEPALIVE_MIN <= keepalive <= KEEPALIVE_MAX:
            keepalive = KEEPALIVE_SECONDS
        return sid, keepalive, session.get('reconnect_url')

    def _subscribe(self, session_id: str) -> None:
        body = {
            'type': 'channel.chat.message', 'version': '1',
            'condition': {'broadcaster_user_id': self._broadcaster_id,
                          'user_id': self._creds['bot_user_id']},
            'transport': {'method': 'websocket', 'session_id': session_id},
        }
        for attempt in (0, 1):
            snap = self._auth_snapshot()
            if snap is None:
                raise _AuthLost()
            resp = self._request('post', f'{HELIX}/eventsub/subscriptions',
                                 headers=self._auth_headers(snap), json=body)
            if resp is None:
                raise _Transient('subscribe failed')
            if resp.status_code == 202:
                return
            if resp.status_code == 401 and attempt == 0:
                if self._ensure_auth(force=True):
                    continue
                raise _AuthLost()
            logger.warning('Twitch chat subscription refused: HTTP %s', resp.status_code)
            raise _Transient('subscribe refused')

    @staticmethod
    def _reconnect_url_ok(url: Any) -> bool:
        """Only the documented EventSub endpoint over TLS: no userinfo, no other
        host, port absent or 443, and one of the documented path forms."""
        if not isinstance(url, str) or not url:
            return False
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError:
            return False
        if parsed.scheme != 'wss' or parsed.hostname != EVENTSUB_HOST:
            return False
        if parsed.username is not None or parsed.password is not None or '@' in parsed.netloc:
            return False
        if port not in (None, 443) or parsed.fragment:
            return False
        return parsed.path in EVENTSUB_PATHS

    def _cycle(self) -> bool:
        """One connection, from connect to loss. True once it was subscribed."""
        if not self._resolve_channel():
            return False
        ws = self._connect(f'{EVENTSUB_URL}?keepalive_timeout_seconds={KEEPALIVE_SECONDS}',
                           WELCOME_TIMEOUT)
        self._ws = ws
        subscribed = False
        try:
            # stop() may have landed while the connector was blocked; it could
            # not close a socket it had not been handed yet.
            if self._stop.is_set():
                return False
            sid, keepalive, _ = self._welcome(ws)
            if self._stop.is_set():
                return False
            try:
                self._subscribe(sid)
            except _AuthLost:
                return False
            subscribed = True
            while not self._stop.is_set():
                if not self._ensure_auth():
                    logger.warning('Twitch token no longer usable; closing the chat session')
                    return subscribed
                frame = self._recv(ws, keepalive + KEEPALIVE_GRACE)
                if frame is None:
                    logger.warning('Twitch keepalive timeout; reconnecting')
                    return subscribed
                new_url = self._dispatch(frame)
                if new_url == '':
                    return subscribed  # revoked
                if new_url:
                    ws = self._follow_reconnect(ws, new_url)
                    keepalive = self._keepalive
            return subscribed
        except _Transient as e:
            logger.warning('Twitch chat link lost: %s', e)
            return subscribed
        finally:
            try:
                (self._ws or ws).close()
            except Exception as e:
                logger.debug('Twitch websocket close failed: %s', type(e).__name__)
            self._ws = None

    def _follow_reconnect(self, old_ws: Any, url: str) -> Any:
        """Open the new link, wait for its welcome, then retire the old one."""
        if not self._reconnect_url_ok(url):
            raise _Transient('unacceptable reconnect url')
        new_ws = self._connect(url, WELCOME_TIMEOUT)
        ok = False
        try:
            if self._stop.is_set():
                raise _Transient('stopping')
            _, self._keepalive, _ = self._welcome(new_ws)
            ok = True
        finally:
            if not ok:
                try:
                    new_ws.close()
                except Exception as e:
                    logger.debug('Twitch websocket close failed: %s', type(e).__name__)
        self._ws = new_ws
        try:
            old_ws.close()
        except Exception as e:
            logger.debug('Twitch websocket close failed: %s', type(e).__name__)
        return new_ws

    def _first_sighting(self, message_id: str) -> bool:
        """Record a notification id; False when it was already seen in the window.

        Bounded by time (REPLAY_WINDOW) and by size (SEEN_IDS). Older replays are
        refused by their timestamp, so eviction cannot reopen them.
        """
        now = self._clock()
        cutoff = now - REPLAY_WINDOW
        seen_at = self._seen.get(message_id)
        if seen_at is not None and seen_at >= cutoff:
            return False
        self._seen.pop(message_id, None)
        while self._seen:
            oldest_at = next(iter(self._seen.values()))
            if oldest_at < cutoff or len(self._seen) >= SEEN_IDS:
                self._seen.popitem(last=False)
            else:
                break
        self._seen[message_id] = now
        return True

    def _dispatch(self, frame: Dict[str, Any]) -> Optional[str]:
        """Handle one frame. Returns a reconnect url, '' for revoked, else None."""
        meta = frame.get('metadata') or {}
        kind = meta.get('message_type')
        payload = frame.get('payload') or {}
        if kind == 'session_reconnect':
            return str((payload.get('session') or {}).get('reconnect_url') or '') or None
        if kind == 'revocation':
            status = (payload.get('subscription') or {}).get('status')
            logger.error('Twitch revoked the chat subscription (%s)', status)
            if status == 'authorization_revoked':
                with self._lock:
                    self._fail_auth('Twitch reports the authorization was revoked')
            return ''
        if kind != 'notification':
            return None
        sent_at = _parse_timestamp(meta.get('message_timestamp'))
        if sent_at is None or time.time() - sent_at > REPLAY_WINDOW:
            if not self._stale_logged:
                self._stale_logged = True
                logger.warning('Twitch notification dropped: timestamp missing or older than '
                               '%d s (is this host clock right?)', int(REPLAY_WINDOW))
            return None
        mid = str(meta.get('message_id') or '')
        if not mid or not self._first_sighting(mid):
            return None
        if meta.get('subscription_type') != 'channel.chat.message':
            return None
        event = payload.get('event')
        if not isinstance(event, dict):
            return None
        if str(event.get('broadcaster_user_id')) != str(self._broadcaster_id):
            return None
        # Shared chat: Twitch delivers other participants' messages to this
        # channel with the origin in source_broadcaster_user_id (null for
        # messages typed here). They are not this channel's conversation, and
        # a user-token send during such a session would reach all of them.
        source = event.get('source_broadcaster_user_id')
        if source not in (None, '') and str(source) != str(self._broadcaster_id):
            self._shared_chat_until = self._clock() + SHARED_CHAT_QUIET
            logger.debug('Twitch shared-chat message from another channel dropped')
            return None
        chat = event_to_chat_event(event, self._creds.get('bot_user_id', ''))
        if chat is None:
            return None
        if chat.channel and not self._broadcaster_login:
            self._broadcaster_login = chat.channel.lower()
        handler = self._handler
        if handler is not None:
            try:
                handler(chat)
            except Exception as e:
                logger.warning('Twitch chat handler raised: %s', type(e).__name__)
        return None

    # ----- send -----------------------------------------------------------

    def send(self, channel: str, text: str, reply_to_id: Optional[str] = None) -> bool:
        try:
            return self._send(channel, text, reply_to_id)
        except Exception as e:
            logger.warning('Twitch send failed: %s', type(e).__name__)
            return False

    def _channel_matches(self, channel: str) -> bool:
        channel = (channel or '').strip().lower().lstrip('#')
        known = {self._broadcaster_id, self._broadcaster_login,
                 self._creds.get('channel', '').lower()}
        return bool(channel) and channel in {k for k in known if k}

    def _rate_ok(self) -> bool:
        now = self._clock()
        if now < self._send_blocked_until:
            return False
        while self._sent_times and now - self._sent_times[0] > SEND_WINDOW_SECONDS:
            self._sent_times.popleft()
        if len(self._sent_times) >= SEND_WINDOW_MAX:
            return False
        if self._sent_times and now - self._sent_times[-1] < SEND_MIN_INTERVAL:
            return False
        return True

    def _send(self, channel: str, text: str, reply_to_id: Optional[str]) -> bool:
        if not self._ensure_auth() or not self._resolve_channel():
            return False
        if not self._channel_matches(channel):
            logger.warning('Twitch send refused: not the configured channel')
            return False
        if self._clock() < self._shared_chat_until:
            logger.warning('Twitch send refused: a shared chat session was seen recently')
            return False
        message = clip_message(text)
        if not message:
            return False
        if not self._allow_command_prefix and has_command_prefix(message):
            logger.warning('Twitch send refused: message starts with a command prefix')
            return False
        with self._lock:
            if not self._rate_ok():
                logger.debug('Twitch send skipped by the local rate limiter')
                return False
            self._sent_times.append(self._clock())
        body: Dict[str, Any] = {'broadcaster_id': self._broadcaster_id, 'message': message}
        if reply_to_id:
            body['reply_parent_message_id'] = str(reply_to_id)
        resp: Any = None
        for attempt in (0, 1):
            snap = self._auth_snapshot()
            if snap is None:
                return False
            body['sender_id'] = snap.bot_user_id
            resp = self._request('post', f'{HELIX}/chat/messages',
                                 headers=self._auth_headers(snap), json=body)
            if resp is None:
                return False
            if resp.status_code == 401 and attempt == 0 and self._ensure_auth(force=True):
                continue
            break
        if resp.status_code == 429:
            with self._lock:
                self._send_blocked_until = self._clock() + self._retry_after(resp)
            return False
        if resp.status_code != 200:
            logger.warning('Twitch send returned HTTP %s', resp.status_code)
            return False
        try:
            item = (resp.json().get('data') or [{}])[0]
        except Exception:
            return False
        if item.get('is_sent') is True:
            return True
        drop = item.get('drop_reason') or {}
        logger.warning('Twitch dropped the message: %s', drop.get('code', 'unknown'))
        return False

    def _retry_after(self, resp: Any) -> float:
        """Seconds to stay quiet after a 429, from Ratelimit-Reset when sane."""
        try:
            reset = float(resp.headers.get('Ratelimit-Reset'))
            wait = reset - time.time()
            if 0 < wait <= 60:
                return wait
        except Exception:
            # No usable Ratelimit-Reset header: fall back to the default pause.
            pass
        return 30.0


__all__: List[str] = ['TwitchChat', 'FrameTooLarge', 'event_to_chat_event', 'clip_message',
                      'has_command_prefix']
