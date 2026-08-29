# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Matrix platform implementation with rich message support.

NOTE: Matrix does NOT support editing messages like Discord.
Messages are posted once and cannot be updated with live viewer counts.
"""

import logging
import re
from typing import Optional
from urllib.parse import quote

import requests

from hypeman_social.config import get_bool_config
from hypeman_social.social.base import SocialPlatform, is_url_for_domain, platform_secret

logger = logging.getLogger(__name__)




class MatrixPlatform(SocialPlatform):
    """
    Matrix platform with rich message support.
    
    NOTE: Matrix does NOT support editing messages like Discord.
    Messages are posted once and cannot be updated with live viewer counts.
    """
    
    def __init__(self, **credentials):
        super().__init__("Matrix", **credentials)
        self.homeserver = None
        self.access_token = None
        self.room_id = None
        self.username = None
        self.password = None
        
    def authenticate(self):
        if not get_bool_config('Matrix', 'enable_posting', default=False):
            return False
        
        # Get homeserver (required)
        self.homeserver = platform_secret('Matrix', 'homeserver')
        
        # Get room ID (required)
        self.room_id = platform_secret('Matrix', 'room_id')
        
        if not self.homeserver or not self.room_id:
            return False
        
        # Ensure homeserver has proper format
        if not self.homeserver.startswith('http'):
            self.homeserver = f"https://{self.homeserver}"
        
        # Check for username/password first (preferred for bot accounts with auto-rotation)
        self.username = platform_secret('Matrix', 'username')
        self.password = platform_secret('Matrix', 'password')
        
        # Priority: Username/Password > Access Token
        # If both are set, username/password takes precedence for automatic token rotation
        if self.username and self.password:
            # Login to get fresh access token
            logger.info("Using username/password authentication (auto-rotation enabled)")
            self.access_token = self._login_and_get_token()
            if not self.access_token:
                logger.error("✗ Matrix login failed - check username/password")
                return False
            logger.info("✓ Matrix logged in and obtained access token")
        else:
            # Fall back to static access token
            logger.info("Using static access token authentication")
            self.access_token = platform_secret('Matrix', 'access_token')
            
            if not self.access_token:
                logger.error("✗ Matrix authentication failed - need either access_token OR username+password")
                return False
        
        self.enabled = True
        self.authenticated = True
        logger.info(f"✓ Matrix authenticated ({self.room_id})")
        return True
    
    def _login_and_get_token(self):
        """Login with username/password to get access token."""
        try:
            # Extract just the username part from full MXID (@username:domain)
            # Matrix login expects just "username", not "@username:domain"
            username_local = self.username
            if username_local.startswith('@'):
                # Remove @ prefix and :domain suffix
                username_local = username_local[1:].split(':')[0]
            
            login_url = f"{self.homeserver}/_matrix/client/r0/login"
            login_data = {
                "type": "m.login.password",
                "identifier": {
                    "type": "m.id.user",
                    "user": username_local
                },
                "password": self.password
            }
            
            response = requests.post(login_url, json=login_data, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                access_token = data.get('access_token')
                if access_token:
                    logger.info(f"✓ Obtained Matrix access token (expires: {data.get('expires_in_ms', 'never')})")
                    return access_token
                else:
                    logger.error("✗ Matrix login succeeded but no access_token in response")
            else:
                logger.error(f"✗ Matrix login failed: {response.status_code}")
            
            return None
        except Exception as e:
            logger.error(f"✗ Matrix login error: {type(e).__name__}: {e}")
            return None
    
    def post(self, message: str, reply_to_id: Optional[str] = None, platform_name: Optional[str] = None, stream_data: Optional[dict] = None) -> Optional[str]:
        if not self.enabled:
            logger.debug(f"⚠ Matrix post skipped: disabled (enabled={self.enabled})")
            return None
        if not all([self.homeserver, self.access_token, self.room_id]):
            logger.warning(f"⚠ Matrix post skipped: missing credentials (homeserver={bool(self.homeserver)}, token={bool(self.access_token)}, room={bool(self.room_id)})")
            return None
            
        try:
            # Extract URL from message for rich formatting
            url_pattern = r'https?://[^\s]+'
            url_match = re.search(url_pattern, message)
            first_url = url_match.group() if url_match else None
            
            # Create rich HTML message with link preview
            html_body = message
            plain_body = message
            
            if first_url:
                # Make URL clickable in HTML
                html_body = re.sub(url_pattern, f'<a href="{first_url}">{first_url}</a>', message)
                
                # Add platform-specific styling
                if is_url_for_domain(first_url, 'twitch.tv'):
                    html_body = f'<p><strong>🟣 Live on Twitch!</strong></p><p>{html_body}</p>'
                elif is_url_for_domain(first_url, 'youtube.com') or is_url_for_domain(first_url, 'youtu.be'):
                    html_body = f'<p><strong>🔴 Live on YouTube!</strong></p><p>{html_body}</p>'
                elif is_url_for_domain(first_url, 'kick.com'):
                    html_body = f'<p><strong>🟢 Live on Kick!</strong></p><p>{html_body}</p>'
            
            # Build Matrix message event
            event_data = {
                "msgtype": "m.text",
                "body": plain_body,
                "format": "org.matrix.custom.html",
                "formatted_body": html_body
            }
            
            # Add reply reference if provided
            if reply_to_id:
                event_data["m.relates_to"] = {
                    "m.in_reply_to": {
                        "event_id": reply_to_id
                    }
                }
            
            # Send message via Matrix Client-Server API
            url = f"{self.homeserver}/_matrix/client/r0/rooms/{quote(self.room_id)}/send/m.room.message"
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json"
            }
            
            response = requests.post(url, json=event_data, headers=headers, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                event_id = data.get('event_id')
                logger.info("✓ Matrix message posted")
                return event_id
            else:
                logger.warning(f"⚠ Matrix post failed with status {response.status_code}: {response.text}")
            return None
        except Exception as e:
            logger.error(f"✗ Matrix post failed: {type(e).__name__}: {e}")
            return None
