# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Mastodon social platform implementation with threading support.
"""

import logging
from typing import Optional

from hypeman_social.config import get_bool_config, get_config
from hypeman_social.social.base import SocialPlatform, platform_secret

# Mastodon.py is the 'mastodon' extra. Importing this module without it must
# not raise — the daemon may only have the extras for the networks it uses.
try:
    from mastodon import Mastodon
    MASTODON_AVAILABLE = True
except ImportError:
    Mastodon = None  # type: ignore[assignment,misc]
    MASTODON_AVAILABLE = False

logger = logging.getLogger(__name__)



class MastodonPlatform(SocialPlatform):
    """Mastodon social platform with threading support."""
    
    def __init__(self, **credentials):
        super().__init__("Mastodon", **credentials)
        self.client = None
        
    def authenticate(self):
        if not get_bool_config('Mastodon', 'enable_posting', default=False):
            return False

        if not MASTODON_AVAILABLE:
            logger.error(
                "✗ Mastodon enabled but Mastodon.py is not installed. "
                "Run: pip install 'hypeman-social[mastodon]'"
            )
            return False
            
        client_id = platform_secret('Mastodon', 'client_id')
        client_secret = platform_secret('Mastodon', 'client_secret')
        access_token = platform_secret('Mastodon', 'access_token')
        api_base_url = get_config('Mastodon', 'api_base_url')
        
        if not all([client_id, client_secret, access_token, api_base_url]):
            missing = []
            if not client_id:
                missing.append('client_id')
            if not client_secret:
                missing.append('client_secret')
            if not access_token:
                missing.append('access_token')
            if not api_base_url:
                missing.append('api_base_url')
            logger.warning(f"✗ Mastodon missing credentials: {', '.join(missing)}")
            return False
            
        try:
            self.client = Mastodon(
                client_id=client_id,
                client_secret=client_secret,
                access_token=access_token,
                api_base_url=api_base_url
            )
            self.enabled = True
            self.authenticated = True
            logger.info("✓ Mastodon authenticated")
            return True
        except Exception as e:
            logger.warning(f"✗ Mastodon authentication failed for {api_base_url}: {type(e).__name__}: {e}")
            return False
    
    @staticmethod
    def _with_repo_card(message: str, repo_data: dict) -> str:
        """
        Append a text rendering of a repository card to a message.

        Mirrors what Bluesky shows in its embed — star count, language, and a
        trimmed description — ported from Star-Daemon's Mastodon connector.
        """
        description = repo_data.get('description', '')
        stars_count = repo_data.get('stargazers_count', 0)
        language = repo_data.get('language', '')

        card_parts = []
        if stars_count:
            card_parts.append(f"⭐ {stars_count:,} stars")
        if language:
            card_parts.append(language)
        card_info = " • ".join(card_parts)

        if description and card_info:
            return f"{message}\n\n{card_info}\n{description[:200]}"
        if description:
            return f"{message}\n\n{description[:200]}"
        if card_info:
            return f"{message}\n\n{card_info}"
        return message

    def post(self, message: str, reply_to_id: Optional[str] = None, platform_name: Optional[str] = None, stream_data: Optional[dict] = None) -> Optional[str]:
        if not self.enabled or not self.client:
            return None
            
        try:
            # Starred-repository posts get a text card appended — Mastodon has
            # no external-link embed API, so the repo metadata rides in the
            # status body the way Star-Daemon's connector rendered it.
            repo_data = stream_data.get('repo_data') if stream_data else None
            if repo_data:
                message = self._with_repo_card(message, repo_data)

            # Check if we should attach a thumbnail image
            media_ids = []
            if stream_data:
                thumbnail_url = stream_data.get('thumbnail_url')
                if not thumbnail_url and repo_data and repo_data.get('owner'):
                    # Repository posts fall back to the owner's avatar
                    thumbnail_url = repo_data['owner'].get('avatar_url')
                if thumbnail_url:
                    try:
                        import os
                        import tempfile

                        import requests
                        
                        # Download thumbnail
                        headers = {
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
                        }
                        img_response = requests.get(thumbnail_url, headers=headers, timeout=10)
                        
                        if img_response.status_code == 200:
                            # Determine file extension from content type or URL
                            content_type = img_response.headers.get('content-type', '')
                            if 'jpeg' in content_type or 'jpg' in content_type or thumbnail_url.endswith('.jpg'):
                                ext = '.jpg'
                            elif 'png' in content_type or thumbnail_url.endswith('.png'):
                                ext = '.png'
                            elif 'webp' in content_type or thumbnail_url.endswith('.webp'):
                                ext = '.webp'
                            else:
                                ext = '.jpg'  # Default fallback
                            
                            # Save to temporary file
                            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp_file:
                                tmp_file.write(img_response.content)
                                tmp_path = tmp_file.name
                            
                            try:
                                # Upload to Mastodon
                                # Build alt-text description for the media
                                if repo_data:
                                    repo_name = repo_data.get('full_name', repo_data.get('name', ''))
                                    description = f"⭐ {repo_name}" if repo_name else "Repository thumbnail"
                                else:
                                    viewer_count = stream_data.get('viewer_count', 0)
                                    game_name = stream_data.get('game_name', '')
                                    description = "🔴 LIVE"
                                    if viewer_count:
                                        description += f" • {viewer_count:,} viewers"
                                    if game_name:
                                        description += f" • {game_name}"

                                media = self.client.media_post(tmp_path, description=description)
                                media_ids.append(media['id'])
                                logger.info(f"✓ Uploaded thumbnail to Mastodon (media ID: {media['id']})")
                            finally:
                                # Clean up temp file
                                os.unlink(tmp_path)
                    except Exception as img_error:
                        logger.warning(f"⚠ Could not upload thumbnail to Mastodon: {img_error}")
            
            # Post as a reply if reply_to_id is provided (threading)
            status = self.client.status_post(
                message, 
                in_reply_to_id=reply_to_id,
                media_ids=media_ids if media_ids else None
            )
            return str(status['id'])
        except Exception as e:
            logger.error(f"✗ Mastodon post failed: {type(e).__name__}: {e}")
            return None
