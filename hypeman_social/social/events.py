# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
The receiving half of a chat platform.

``SocialPlatform`` shouts: it authenticates and posts. A conversational bot also
has to *hear*, so a platform that supports chat adds a ``ChatReceiver`` that
turns whatever the network delivers into one ``ChatEvent`` and sends a reply
back. The bot's logic (who it is, when it speaks, what it is allowed to say)
lives in the consumer, never in an adapter: an adapter converts events in and
sends text out, and holds no policy and no model access.

Two rules carry over from the rest of hypeman. ``is_available()`` is the health
check and is allowed to heal; nothing gates on an ``enabled`` flag. And
``send()`` never raises: a failed send is a ``False``, because degradation must
never become downtime.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, Optional

logger = logging.getLogger(__name__)

#: Roles an adapter may report for an author. An adapter reports only what the
#: platform itself says about the sender, resolved by the platform's stable user
#: id, never by display name.
ROLE_BROADCASTER = 'broadcaster'
ROLE_MODERATOR = 'moderator'
ROLE_SUBSCRIBER = 'subscriber'
ROLE_VIP = 'vip'


@dataclass(frozen=True)
class ChatEvent:
    """One message heard on a platform, in the same shape for every platform."""

    #: Lowercase platform name, matching the ``REGISTRY`` key (``'twitch'``).
    platform: str
    #: Where it was said: a Twitch channel login, a Discord channel id.
    channel: str
    #: The platform's id for this message. Consumers use it to drop messages
    #: they already sent and to deduplicate across a bridge.
    message_id: str
    #: The platform's stable id for the sender. Never a display name.
    author_id: str
    author_name: str
    text: str
    #: True when the sender is this bot's own account.
    is_self: bool = False
    #: True when the message was relayed by a bridge (a webhook, a ghost or
    #: puppet user) rather than typed by a native user of this platform.
    bridged: bool = False
    #: Roles the platform reports for the sender (``ROLE_*``).
    roles: FrozenSet[str] = frozenset()
    #: The message this one replies to, when the platform says so.
    reply_to_id: Optional[str] = None
    #: Anything platform-specific an adapter wants to pass along as strings.
    extra: Dict[str, str] = field(default_factory=dict)


#: Called once per event, from the receiver's own thread. A handler that
#: raises is logged and the receiver keeps running.
ChatHandler = Callable[[ChatEvent], None]


class ChatReceiver(ABC):
    """Hears a platform's chat and replies to it. One adapter per platform."""

    #: Lowercase platform name, matching ``ChatEvent.platform``.
    name: str = ''

    @abstractmethod
    def is_available(self) -> bool:
        """Whether the platform can be used right now; allowed to heal."""

    @abstractmethod
    def start(self, handler: ChatHandler) -> None:
        """Begin delivering events to ``handler`` on a background thread."""

    @abstractmethod
    def stop(self) -> None:
        """Stop delivering events and release the connection."""

    @abstractmethod
    def send(self, channel: str, text: str, reply_to_id: Optional[str] = None) -> bool:
        """Send ``text`` to ``channel``. Returns False on failure; never raises."""
