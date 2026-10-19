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
* Twitch requires the token to be validated at start and hourly. A token that
  cannot be validated or refreshed marks the adapter unavailable with one clear
  error, then a single cooldown-guarded retry per cooldown, never a storm.
* The bot only ever speaks in the one channel it was configured for.
* A refreshed token is held in memory only. There is deliberately no Doppler
  write path here (it would need a write-scoped Doppler token), so after a
  restart the stored tokens are used again; see docs/TWITCH_BOT.md.
"""

import collections
import json
import logging
import threading
import time
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

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
    websocket = None

logger = logging.getLogger(__name__)

EVENTSUB_URL = 'wss://eventsub.wss.twitch.tv/ws'
VALIDATE_URL = 'https://id.twitch.tv/oauth2/validate'
TOKEN_URL = 'https://id.twitch.tv/oauth2/token'
HELIX = 'https://api.twitch.tv/helix'

#: The documented valid range is 10 to 600; 30 keeps dead links short.
KEEPALIVE_SECONDS = 30
#: Slack on top of the keepalive window before a silent link is declared dead.
KEEPALIVE_GRACE = 5
#: Twitch closes a connection that has not subscribed within 10 seconds.
WELCOME_TIMEOUT = 10

#: Twitch requires validation at start and hourly; re-check a little early.
VALIDATE_INTERVAL = 3300
#: Refresh ahead of expiry when validation shows less than this left.
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

MAX_FRAME_BYTES = 1024 * 1024
SEEN_IDS = 512

REQUIRED_SCOPES = ('user:read:chat', 'user:write:chat')

_ROLE_BADGES = {
    'broadcaster': ROLE_BROADCASTER,
    'moderator': ROLE_MODERATOR,
    'subscriber': ROLE_SUBSCRIBER,
    'vip': ROLE_VIP,
}


class _AuthLost(Exception):
    """The token can no longer be used; end the session."""


class _Transient(Exception):
    """A failure worth another try after backoff."""


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError) or type(exc).__name__ == 'WebSocketTimeoutException'


def _default_connect(url: str, timeout: float) -> Any:
    return websocket.create_connection(url, timeout=timeout)


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
    """One line, at most MAX_MESSAGE_CHARS, cut at a word when that is cheap."""
    text = ' '.join(str(text).split())
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    cut = text[:MAX_MESSAGE_CHARS - 1]
    space = cut.rfind(' ')
    if space >= MAX_MESSAGE_CHARS - 60:
        cut = cut[:space]
    return cut.rstrip() + '…'


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
    ):
        self._http = http if http is not None else requests.Session()
        self._connect = connect or _default_connect
        self._clock = clock
        self._backoff_base = backoff_base
        self._long_rest = long_rest
        self._auth_poll = auth_poll
        self._keepalive = KEEPALIVE_SECONDS

        self._lock = threading.RLock()
        self._creds: Dict[str, str] = {}
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._validated_at: Optional[float] = None
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
        self._seen: Deque[str] = collections.deque(maxlen=SEEN_IDS)
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
        with self._lock:
            self._creds = creds
            if self._access_token is None or self._auth_failed_at is not None:
                self._access_token = creds['access_token'] or None
                self._refresh_token = creds['refresh_token'] or None
        missing = [k for k in ('client_id', 'access_token', 'bot_user_id', 'channel')
                   if not creds[k]]
        if missing:
            if not self._missing_logged:
                self._missing_logged = True
                logger.error('Twitch chat is not configured; missing: %s', ', '.join(missing))
            return False
        self._missing_logged = False
        return True

    def _auth_headers(self) -> Dict[str, str]:
        return {'Authorization': f'Bearer {self._access_token}',
                'Client-Id': self._creds.get('client_id', '')}

    def _fail_auth(self, reason: str) -> None:
        self._auth_failed_at = self._clock()
        self._validated_at = None
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

    def _validate(self) -> Tuple[str, int]:
        """('ok'|'invalid'|'bad'|'transient', expires_in). 'bad' = wrong account."""
        resp = self._request('get', VALIDATE_URL,
                             headers={'Authorization': f'OAuth {self._access_token}'})
        if resp is None:
            return 'transient', 0
        if resp.status_code == 401:
            return 'invalid', 0
        if resp.status_code != 200:
            logger.warning('Twitch token validation returned HTTP %s', resp.status_code)
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
        return 'ok', int(body.get('expires_in') or 0)

    def _refresh(self) -> bool:
        if not (self._refresh_token and self._creds.get('client_secret')):
            return False
        resp = self._request('post', TOKEN_URL, data={
            'grant_type': 'refresh_token',
            'refresh_token': self._refresh_token,
            'client_id': self._creds['client_id'],
            'client_secret': self._creds['client_secret'],
        })
        if resp is None or resp.status_code != 200:
            if resp is not None:
                logger.warning('Twitch token refresh returned HTTP %s', resp.status_code)
            return False
        try:
            body = resp.json()
            new_access = body['access_token']
            new_refresh = body.get('refresh_token') or self._refresh_token
        except Exception:
            return False
        self._access_token = new_access
        self._refresh_token = new_refresh
        if not self._memory_only_noted:
            self._memory_only_noted = True
            logger.warning('Twitch tokens were refreshed and are held in memory only; the '
                           'stored copies are not updated (see docs/TWITCH_BOT.md).')
        return True

    def _ensure_auth(self, force: bool = False) -> bool:
        """True when the token is validated and fresh; heals within cooldowns."""
        with self._lock:
            now = self._clock()
            if self._auth_failed_at is not None:
                if now - self._auth_failed_at < AUTH_COOLDOWN:
                    return False
            elif (not force and self._validated_at is not None
                  and now - self._validated_at < VALIDATE_INTERVAL):
                return True
            elif (self._last_attempt is not None and now - self._last_attempt < ATTEMPT_COOLDOWN
                  and not force):
                return self._recently_valid(now)
            self._last_attempt = now

            if not self._load_credentials():
                return False
            verdict, expires = self._validate()
            if verdict == 'invalid' or (verdict == 'ok' and expires < REFRESH_MARGIN):
                if self._refresh():
                    verdict, expires = self._validate()
                elif verdict == 'invalid':
                    self._fail_auth('the access token is invalid and could not be refreshed')
                    return False
            if verdict == 'ok':
                self._validated_at = self._clock()
                self._auth_failed_at = None
                self._auth_error_logged = False
                return True
            if verdict == 'invalid':
                self._fail_auth('the access token is invalid after a refresh')
                return False
            if verdict == 'bad':
                return False
            return self._recently_valid(now)

    def _recently_valid(self, now: float) -> bool:
        """A transient validation failure does not kill a token that just worked."""
        return self._validated_at is not None and now - self._validated_at < 2 * VALIDATE_INTERVAL

    def _resolve_channel(self) -> bool:
        if self._broadcaster_id:
            return True
        channel = self._creds.get('channel', '')
        if channel.isdigit():
            self._broadcaster_id = channel
            return True
        resp = self._request('get', f'{HELIX}/users', headers=self._auth_headers(),
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
                return
            self._stop.clear()
            self._spawn()

    def _spawn(self) -> None:
        self._thread = threading.Thread(target=self._serve, name='twitch-chat', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception as e:
                logger.debug('Twitch websocket close failed: %s', type(e).__name__)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._thread = None
        self._ws = None

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
        try:
            raw = ws.recv()
        except Exception as e:
            if _is_timeout(e):
                return None
            raise _Transient('websocket closed') from None
        if not raw:
            raise _Transient('websocket closed')
        if len(raw) > MAX_FRAME_BYTES:
            logger.warning('Twitch frame over %d bytes dropped', MAX_FRAME_BYTES)
            return {}
        try:
            frame = json.loads(raw)
        except ValueError:
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
        keepalive = int(session.get('keepalive_timeout_seconds') or KEEPALIVE_SECONDS)
        return sid, keepalive, session.get('reconnect_url')

    def _subscribe(self, session_id: str) -> None:
        body = {
            'type': 'channel.chat.message', 'version': '1',
            'condition': {'broadcaster_user_id': self._broadcaster_id,
                          'user_id': self._creds['bot_user_id']},
            'transport': {'method': 'websocket', 'session_id': session_id},
        }
        for attempt in (0, 1):
            resp = self._request('post', f'{HELIX}/eventsub/subscriptions',
                                 headers=self._auth_headers(), json=body)
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
        try:
            parsed = urlparse(str(url))
        except ValueError:
            return False
        host = parsed.hostname or ''
        return parsed.scheme == 'wss' and (host == 'twitch.tv' or host.endswith('.twitch.tv'))

    def _cycle(self) -> bool:
        """One connection, from connect to loss. True once it was subscribed."""
        if not self._resolve_channel():
            return False
        ws = self._connect(f'{EVENTSUB_URL}?keepalive_timeout_seconds={KEEPALIVE_SECONDS}',
                           WELCOME_TIMEOUT)
        self._ws = ws
        subscribed = False
        try:
            sid, keepalive, _ = self._welcome(ws)
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

    def _follow_reconnect(self, old_ws: Any, url: str) -> Any:
        """Open the new link, wait for its welcome, then retire the old one."""
        if not self._reconnect_url_ok(url):
            raise _Transient('unacceptable reconnect url')
        new_ws = self._connect(url, WELCOME_TIMEOUT)
        try:
            _, self._keepalive, _ = self._welcome(new_ws)
        except BaseException:
            try:
                new_ws.close()
            except Exception as e:
                logger.debug('Twitch websocket close failed: %s', type(e).__name__)
            raise
        self._ws = new_ws
        try:
            old_ws.close()
        except Exception as e:
            logger.debug('Twitch websocket close failed: %s', type(e).__name__)
        return new_ws

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
        mid = meta.get('message_id')
        if mid:
            if mid in self._seen:
                return None
            self._seen.append(mid)
        if meta.get('subscription_type') != 'channel.chat.message':
            return None
        event = payload.get('event')
        if not isinstance(event, dict):
            return None
        if str(event.get('broadcaster_user_id')) != str(self._broadcaster_id):
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
        message = clip_message(text)
        if not message:
            return False
        with self._lock:
            if not self._rate_ok():
                logger.debug('Twitch send skipped by the local rate limiter')
                return False
            self._sent_times.append(self._clock())
        body: Dict[str, Any] = {'broadcaster_id': self._broadcaster_id,
                                'sender_id': self._creds['bot_user_id'], 'message': message}
        if reply_to_id:
            body['reply_parent_message_id'] = str(reply_to_id)
        for attempt in (0, 1):
            resp = self._request('post', f'{HELIX}/chat/messages',
                                 headers=self._auth_headers(), json=body)
            if resp is None:
                return False
            if resp.status_code == 401 and attempt == 0 and self._ensure_auth(force=True):
                continue
            break
        if resp.status_code == 429:
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
            pass
        return 30.0


__all__: List[str] = ['TwitchChat', 'event_to_chat_event', 'clip_message']
