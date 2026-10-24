# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
The one definition of "this value was never filled in".

Every secret lookup in the library, and every daemon built on it, goes through
is_placeholder(), so a template value is treated exactly like an unset one: the
feature stays disabled and nothing is sent to the real service.

The rule is case-insensitive, applied to the whole value (surrounding space
ignored) and to each segment of a URL-shaped value, so a webhook URL whose
token is still ``YOUR_WEBHOOK_TOKEN`` is caught too. A value is a placeholder
when it is:

* empty or whitespace only
* wrapped in angle brackets: ``<token>``
* exactly ``example`` or ``placeholder``
* starting with ``your_``, ``your-``, ``changeme``, ``change_me``,
  ``change-me``, ``xxx`` or ``dp.st.your``
"""

import re
from typing import Any

_PREFIXES = (
    'your_', 'your-', 'changeme', 'change_me', 'change-me', 'xxx', 'dp.st.your',
)
_EXACT = frozenset({'example', 'placeholder'})
_BRACKETED = re.compile(r'^<[^<>]*>$')
# Splits a URL-shaped value into the pieces a token can hide in.
_SEGMENTS = re.compile(r'[/?&=:@#]+')


def _segment_is_placeholder(segment: str) -> bool:
    if _BRACKETED.match(segment) or segment in _EXACT:
        return True
    return segment.startswith(_PREFIXES)


def is_placeholder(value: Any) -> bool:
    """True when the value is unset, empty or an unedited template placeholder."""
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    text = value.strip().lower()
    if not text:
        return True
    if _segment_is_placeholder(text):
        return True
    parts = [p for p in _SEGMENTS.split(text) if p]
    return len(parts) > 1 and any(_segment_is_placeholder(p) for p in parts)
