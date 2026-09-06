# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Threads (Meta) platform implementation via the Threads Graph API.

Threads publishing is a two-step dance: create a media container, then
publish it. Both are plain HTTPS calls, so — like Discord and Matrix — this
platform needs no extra to install.

Setup requires a Meta app with the Threads API enabled and a long-lived user
access token; see docs/CONFIGURATION.md for the walkthrough. Configure with:

    THREADS_ENABLE_POSTING=true
    THREADS_ACCESS_TOKEN=...   # long-lived token with threads_basic + threads_content_publish
    THREADS_USER_ID=...        # numeric Threads user ID
"""

import logging
import re
from typing import Optional

import requests

from hypeman_social.config import get_bool_config
from hypeman_social.social.base import SocialPlatform

logger = logging.getLogger(__name__)

#: Threads' documented post length limit.
THREADS_CHAR_LIMIT = 500

API_BASE = 'https://graph.threads.net/v1.0'


class ThreadsPlatform(SocialPlatform):
    """
    Threads platform using the official Graph API (container create + publish).

    Threading is supported: pass reply_to_id (a previously returned media ID)
    to post a reply, same as every other platform here.
    """

    char_limit = THREADS_CHAR_LIMIT

    def __init__(self, **credentials):
        super().__init__("Threads", **credentials)
        self.access_token: Optional[str] = None
        self.user_id: Optional[str] = None

    def authenticate(self):
        if not get_bool_config('Threads', 'enable_posting', default=False):
            return False

        self.access_token = self.credential('access_token')
        self.user_id = self.credential('user_id')

        if not self.access_token or not self.user_id:
            missing = []
            if not self.access_token:
                missing.append('access_token')
            if not self.user_id:
                missing.append('user_id')
            logger.warning(f"✗ Threads missing credentials: {', '.join(missing)}")
            return False

        self.enabled = True
        self.authenticated = True
        logger.info("✓ Threads configured")
        return True

    def test_connection(self) -> bool:
        """Cheap probe: ask the API who this token belongs to."""
        if not self.is_ready():
            return False
        try:
            response = requests.get(
                f"{API_BASE}/me",
                params={'fields': 'id', 'access_token': self.access_token},
                timeout=10,
            )
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"⚠ Threads connection test failed: {type(e).__name__}: {e}")
            return False

    def post(self, message: str, reply_to_id: Optional[str] = None, platform_name: Optional[str] = None, stream_data: Optional[dict] = None) -> Optional[str]:
        if not self.enabled or not self.access_token or not self.user_id:
            return None

        # Enforce the platform limit; keep a trailing URL intact if present.
        if len(message) > THREADS_CHAR_LIMIT:
            logger.warning(
                f"Threads message too long ({len(message)} chars), trimming to {THREADS_CHAR_LIMIT}")
            message = self._truncate_to_limit(message, THREADS_CHAR_LIMIT)

        try:
            # Step 1: create the media container.
            params = {
                'media_type': 'TEXT',
                'text': message,
                'access_token': self.access_token,
            }
            if reply_to_id:
                params['reply_to_id'] = reply_to_id

            # Attach the first URL as an explicit link preview card. Threads
            # only previews a link when told to; without this the post is
            # plain text.
            url_match = re.search(r'https?://[^\s]+', message)
            if url_match:
                params['link_attachment'] = url_match.group()

            create_response = requests.post(
                f"{API_BASE}/{self.user_id}/threads", data=params, timeout=15)

            if create_response.status_code != 200:
                logger.warning(
                    f"⚠ Threads container creation failed with status "
                    f"{create_response.status_code}: {create_response.text[:200]}")
                return None

            creation_id = create_response.json().get('id')
            if not creation_id:
                logger.warning("⚠ Threads container creation returned no ID")
                return None

            # Step 2: publish the container.
            publish_response = requests.post(
                f"{API_BASE}/{self.user_id}/threads_publish",
                data={'creation_id': creation_id, 'access_token': self.access_token},
                timeout=15,
            )

            if publish_response.status_code != 200:
                logger.warning(
                    f"⚠ Threads publish failed with status "
                    f"{publish_response.status_code}: {publish_response.text[:200]}")
                return None

            media_id = publish_response.json().get('id')
            logger.info(f"✓ Threads post published (ID: {media_id})")
            return media_id
        except Exception as e:
            logger.error(f"✗ Threads post failed: {type(e).__name__}: {e}")
            return None

    @staticmethod
    def _truncate_to_limit(message: str, limit: int) -> str:
        """Trim to the limit, preserving a URL if the message carries one."""
        if len(message) <= limit:
            return message

        url_match = re.search(r'https?://[^\s]+', message)
        url = url_match.group() if url_match else ''
        url_space = len(url) + 2 if url else 0  # +2 for the blank line

        content = re.sub(r'\n*https?://[^\s]+\s*', '', message).strip()
        max_content = limit - url_space - 3  # -3 for "..."
        if max_content < 10:
            return message[:limit]

        if len(content) > max_content:
            truncated = content[:max_content]
            last_space = truncated.rfind(' ')
            if last_space > max_content // 2:
                truncated = truncated[:last_space]
            content = truncated + '...'

        return f"{content}\n\n{url}" if url else content
