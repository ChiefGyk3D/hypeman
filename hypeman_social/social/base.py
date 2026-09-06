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
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from hypeman_social.config import get_secret

logger = logging.getLogger(__name__)


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
            stream_data: Extra context — title, url, thumbnail, event_kind.

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
