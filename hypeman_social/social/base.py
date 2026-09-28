# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Base class and shared helpers for social platforms.

Every platform here answers the same two questions: can you authenticate, and
can you post. Everything else — threading, embeds, character limits, whether
the network calls it a "toot" — is the platform's own business.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from hypeman_social.config import get_secret

logger = logging.getLogger(__name__)

#: Most images one post may carry. Bluesky and Mastodon both stop at four;
#: anything past that is dropped with a warning rather than failing the post.
MAX_IMAGES_PER_POST = 4

#: Image formats recognised by magic number, so a caller handing over raw
#: bytes (a chart rendered in memory) never has to name the type itself.
_IMAGE_SIGNATURES = (
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'GIF87a', 'image/gif'),
    (b'GIF89a', 'image/gif'),
)

_BROWSER_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36'
)


#: What kind of thing is being announced. Platforms use this to pick embed
#: colours, titles, and which metadata fields make sense.
#:
#: This is passed explicitly rather than inferred from the platform name,
#: because "youtube" is ambiguous: Boon-Tube means a new upload, stream-daemon
#: means a live broadcast. Guessing got that wrong.
EVENT_UPLOAD = 'upload'   # A video or short was posted
EVENT_LIVE = 'live'       # A live stream started
EVENT_END = 'end'         # A live stream ended
EVENT_STAR = 'star'       # A repository was starred


def platform_secret(platform: str, key: str, default: Optional[str] = None) -> Optional[str]:
    """
    Fetch a platform credential using the conventional secret-manager env names.

    Saves every platform module from repeating the same three kwargs on every
    single lookup:

        SECRETS_AWS_<PLATFORM>_SECRET_NAME
        SECRETS_VAULT_<PLATFORM>_SECRET_PATH
        SECRETS_DOPPLER_<PLATFORM>_SECRET_NAME

    Args:
        platform: Platform name, e.g. 'Discord', 'Matrix'.
        key: Credential key, e.g. 'webhook_url'.
        default: Value if nothing is configured.

    Returns:
        The credential, or the default.
    """
    upper = platform.upper()
    return get_secret(
        platform,
        key,
        default,
        secret_name_env=f'SECRETS_AWS_{upper}_SECRET_NAME',
        secret_path_env=f'SECRETS_VAULT_{upper}_SECRET_PATH',
        doppler_secret_env=f'SECRETS_DOPPLER_{upper}_SECRET_NAME',
    )


def sniff_image_mime(data: bytes, fallback: str = 'image/png') -> str:
    """
    Work out an image's media type from its first bytes.

    WebP is RIFF-framed, so it needs a second look past the container header;
    everything else is a plain prefix match. Unknown data gets the fallback.
    """
    for signature, mime in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return mime
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return fallback


def attached_images(stream_data: Optional[dict]) -> List[Dict[str, Any]]:
    """
    Resolve ``stream_data['images']`` into ready-to-upload attachments.

    Each entry describes one picture, either as bytes the caller already has
    (``{'data': b'...', 'alt': 'GOES X-ray flux, past 6 hours'}``) or as a URL
    to fetch (``{'url': 'https://...', 'alt': '...'}``). An optional
    ``mime_type`` overrides detection. The result is a list of dicts with
    ``data``, ``alt`` and ``mime_type`` filled in, in the order given.

    The download lives here so Bluesky and Mastodon share one code path and
    one set of failure rules: an entry that cannot be resolved is logged and
    dropped, never raised, because a missing picture must not cost the post.
    Anything past MAX_IMAGES_PER_POST is dropped the same way.

    This is separate from ``thumbnail_url``, which drives link cards and
    embed thumbnails for an announcement about a URL. ``images`` is for posts
    whose pictures *are* the content: a rendered chart, a downloaded map.
    """
    if not stream_data:
        return []

    entries = stream_data.get('images') or []
    if not isinstance(entries, (list, tuple)):
        logger.warning("⚠ stream_data['images'] must be a list; ignoring it")
        return []

    resolved: List[Dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if len(resolved) >= MAX_IMAGES_PER_POST:
            logger.warning(
                f"⚠ Post carries more than {MAX_IMAGES_PER_POST} images; dropping the rest"
            )
            break

        if not isinstance(entry, dict):
            logger.warning(f"⚠ Image {index + 1} is not a dict; skipping")
            continue

        data = entry.get('data')
        url = entry.get('url')
        mime_type = entry.get('mime_type')

        if not data and url:
            try:
                import requests

                response = requests.get(
                    url, headers={'User-Agent': _BROWSER_USER_AGENT}, timeout=15)
                if response.status_code == 200 and response.content:
                    data = response.content
                    if not mime_type:
                        header = response.headers.get('content-type', '').split(';')[0]
                        mime_type = header.strip() or None
                else:
                    logger.warning(
                        f"⚠ Image {index + 1} download returned {response.status_code}; skipping"
                    )
            except Exception as e:
                logger.warning(
                    f"⚠ Image {index + 1} download failed: {type(e).__name__}: {e}"
                )

        if not data:
            logger.warning(f"⚠ Image {index + 1} has no data; skipping")
            continue

        if not isinstance(data, (bytes, bytearray)):
            logger.warning(f"⚠ Image {index + 1} data is not bytes; skipping")
            continue

        data = bytes(data)
        mime_type = mime_type or sniff_image_mime(data)
        resolved.append({
            'data': data,
            'alt': str(entry.get('alt') or ''),
            'mime_type': mime_type,
        })

    return resolved


def is_url_for_domain(url: str, domain: str) -> bool:
    """
    Check whether a URL belongs to a domain, without the classic substring bug.

    `'youtube.com' in url` happily matches `evil-youtube.com.attacker.net`.
    This parses the host and checks it properly.

    Args:
        url: The URL to test.
        domain: Bare domain, e.g. 'youtube.com'.

    Returns:
        True if the URL's host is that domain or a subdomain of it.
    """
    if not url:
        return False

    try:
        host = (urlparse(url).hostname or '').lower()
    except (ValueError, AttributeError):
        return False

    domain = domain.lower()
    return host == domain or host.endswith(f'.{domain}')


class SocialPlatform(ABC):
    """
    A place to shout about your content.

    Subclasses implement authenticate() and post(). The rest — readiness
    checks, health status, safe posting — comes for free.

    Credentials may be passed to __init__ to override config lookup. That keeps
    the config-driven style the daemons use while allowing explicit injection
    for tests and for daemons that prefer wiring credentials themselves.
    """

    #: Platform character limit. None means "no meaningful limit".
    char_limit: Optional[int] = None

    def __init__(self, name: str, enabled: bool = False, **credentials: Any):
        self.name = name
        self.enabled = enabled
        self.authenticated = False
        self._credential_overrides = {k: v for k, v in credentials.items() if v is not None}
        self._last_error: Optional[str] = None

    def credential(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """
        Get a credential, preferring an explicit constructor override.

        Falls back to the secret manager / env lookup when nothing was injected.
        """
        if key in self._credential_overrides:
            return self._credential_overrides[key]
        return platform_secret(self.name, key, default)

    @abstractmethod
    def authenticate(self) -> bool:
        """Establish credentials. Returns True on success."""

    @abstractmethod
    def post(
        self,
        message: str,
        reply_to_id: Optional[str] = None,
        platform_name: Optional[str] = None,
        stream_data: Optional[dict] = None,
    ) -> Optional[str]:
        """
        Post a message.

        Args:
            message: The text to post.
            reply_to_id: ID of a previous post to thread under, if any.
            platform_name: Source platform the announcement is about.
            stream_data: Extra context — title, url, thumbnail, event_kind,
                and ``images`` (see attached_images) for posts that carry
                pictures of their own.

        Returns:
            The new post's ID (for threading), or None on failure.
        """

    def is_ready(self) -> bool:
        """True if this platform is enabled and authenticated."""
        return self.enabled and self.authenticated

    def test_connection(self) -> bool:
        """
        Verify the platform still answers.

        Defaults to the readiness flags. Platforms that can cheaply probe their
        API should override this so health checks mean something.
        """
        return self.is_ready()

    def safe_post(self, message: str, **kwargs: Any) -> Optional[str]:
        """
        Post without ever raising.

        One social network having a bad day should not take down the daemon or
        stop the other platforms from getting their announcement.
        """
        if not self.is_ready():
            logger.debug(f"⊘ {self.name} not ready, skipping post")
            return None

        try:
            return self.post(message, **kwargs)
        except Exception as e:
            self._last_error = f"{type(e).__name__}: {e}"
            logger.error(f"✗ {self.name} post failed: {type(e).__name__}: {e}")
            return None

    def status(self) -> Dict[str, Any]:
        """Machine-readable state, for the health endpoint."""
        return {
            'name': self.name,
            'enabled': self.enabled,
            'authenticated': self.authenticated,
            'last_error': self._last_error,
        }


def event_kind(stream_data: Optional[dict], default: str = EVENT_LIVE) -> str:
    """
    Read the event kind out of a payload, falling back to a caller default.

    Args:
        stream_data: The payload passed to post().
        default: What to assume when the payload doesn't say.

    Returns:
        One of EVENT_UPLOAD, EVENT_LIVE, EVENT_END, EVENT_STAR.
    """
    if stream_data and stream_data.get('event_kind'):
        return stream_data['event_kind']
    return default
