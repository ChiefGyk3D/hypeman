# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Social platforms: the places hypeman shouts."""

from hypeman_social.social.base import (
    EVENT_END,
    EVENT_LIVE,
    EVENT_STAR,
    EVENT_UPLOAD,
    SocialPlatform,
    event_kind,
    is_url_for_domain,
    platform_secret,
)
from hypeman_social.social.bluesky import BlueskyPlatform
from hypeman_social.social.discord import DiscordPlatform
from hypeman_social.social.mastodon import MastodonPlatform
from hypeman_social.social.matrix import MatrixPlatform

#: Every platform hypeman can post to, by lowercase name.
#:
#: Adding a network (Threads is the next one planned) means writing the module
#: and adding one line here — both daemons pick it up without further changes.
REGISTRY = {
    'bluesky': BlueskyPlatform,
    'mastodon': MastodonPlatform,
    'discord': DiscordPlatform,
    'matrix': MatrixPlatform,
}

__all__ = [
    'SocialPlatform',
    'BlueskyPlatform',
    'MastodonPlatform',
    'DiscordPlatform',
    'MatrixPlatform',
    'REGISTRY',
    'EVENT_UPLOAD',
    'EVENT_LIVE',
    'EVENT_END',
    'EVENT_STAR',
    'event_kind',
    'is_url_for_domain',
    'platform_secret',
]
