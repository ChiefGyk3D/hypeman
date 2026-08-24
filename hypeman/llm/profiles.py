# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Content profiles: the domain vocabulary that differs between announcement types.

Announcing "I went live on Twitch" and "I posted a video on YouTube" need the
same guardrails but different word lists. A stream announcement hallucinating
"VOD coming soon" is a different failure than a video announcement hallucinating
"live now". Rather than fork the guardrails, we parameterize them.

Add your own profile if you're announcing something else entirely.
"""

from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class ContentProfile:
    """
    Domain vocabulary for one kind of announcement.

    Attributes:
        name: Human-readable profile name, used in log messages.
        content_noun: What the thing is, e.g. 'video' or 'stream'. Appears in
            quality-check feedback like "Doesn't reference stream title".
        generic_phrases: Filler phrases that make a post sound like every other
            post. Each occurrence costs quality score.
        hallucination_patterns: Regexes for details a model tends to invent that
            were never in the source data. Any match fails validation outright,
            because posting "giveaway tonight at 8pm" when there is no giveaway
            is worse than posting nothing.
    """

    name: str
    content_noun: str
    generic_phrases: List[str] = field(default_factory=list)
    hallucination_patterns: List[str] = field(default_factory=list)


# For daemons announcing uploaded videos and shorts (Boon-Tube-Daemon).
VIDEO_PROFILE = ContentProfile(
    name='video',
    content_noun='video',
    generic_phrases=[
        'check it out',
        "let's go",
        'new video',
        'just dropped',
        'link below',
        'video time',
        'must watch',
    ],
    hallucination_patterns=[
        r'drops?\s+enabled',
        r'giveaway',
        r'tonight\s+at\s+\d',
        r'premiering?\s+at\s+\d',
        r'\d+\s*pm',
        r'\d+\s*am',
        r'coming\s+soon',
        r'\d+\s+views?',
        r'subscribe\s+for',
        r'special\s+guest',
        r'live\s+now',
        r'streaming\s+now',
    ],
)


# For daemons announcing live streams (stream-daemon).
STREAM_PROFILE = ContentProfile(
    name='stream',
    content_noun='stream',
    generic_phrases=[
        'come hang out',
        "let's go",
        'join me',
        'thanks for watching',
        'see you next time',
        'stream time',
        'going live',
    ],
    hallucination_patterns=[
        r'drops?\s+enabled',
        r'giveaway',
        r'tonight\s+at\s+\d',
        r'starting\s+at\s+\d',
        r'\d+\s*pm',
        r'\d+\s*am',
        r'vod\s+coming',
        r'vod\s+soon',
        r'next\s+stream',
        r'\d+\s+viewers?',
        r'raided?\s+',
        r'special\s+guest',
        r'new\s+video',
    ],
)


# Neutral fallback for anything else.
GENERIC_PROFILE = ContentProfile(
    name='generic',
    content_noun='content',
    generic_phrases=["let's go", 'check it out', 'link below'],
    hallucination_patterns=[r'giveaway', r'\d+\s*pm', r'\d+\s*am', r'special\s+guest'],
)
