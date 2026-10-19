# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Shared helpers for the cloud API providers (Claude, OpenAI).

Kept in one place so both providers throttle and classify errors identically.
Gemini predates this module and carries its own copy of the throttle; it is
left alone on purpose.
"""

import threading
import time
from typing import Callable, Optional


class RateLimiter:
    """
    Client-side requests-per-minute limiter, safe to share across threads.

    A limit of 0 (or less) disables throttling. Spacing calls evenly is
    coarser than a token bucket but cheap, predictable, and enough to stay
    under a provider's published requests-per-minute without discovering the
    limit via 429s.
    """

    def __init__(
        self,
        per_minute: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.per_minute = per_minute
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_call: Optional[float] = None

    def wait(self) -> None:
        """Block until the next call is allowed."""
        if self.per_minute <= 0:
            return
        min_interval = 60.0 / self.per_minute
        with self._lock:
            now = self._clock()
            if self._last_call is not None:
                elapsed = now - self._last_call
                if elapsed < min_interval:
                    self._sleep(min_interval - elapsed)
                    now = self._clock()
            self._last_call = now


def classify_sdk_error(error: BaseException) -> str:
    """
    Turn an SDK exception into a short, key-free message the base class can
    classify by substring.

    The original text is deliberately dropped: SDK error bodies can echo
    request details, and the base class logs and stores what we raise. Works
    on class names (walking the MRO) and an optional ``status_code`` so it
    needs no SDK import and behaves the same for both vendors.
    """
    names = {cls.__name__ for cls in type(error).__mro__}
    status = getattr(error, 'status_code', None)

    # Timeout first: in both SDKs it subclasses the connection error.
    if 'APITimeoutError' in names or 'Timeout' in names or 'TimeoutException' in names:
        return 'request timed out'
    if 'RateLimitError' in names or status == 429:
        return 'rate limit (429)'
    if 'AuthenticationError' in names or status == 401:
        return 'unauthorized (401)'
    if 'PermissionDeniedError' in names or status == 403:
        return 'forbidden (403)'
    if 'NotFoundError' in names or status == 404:
        return 'not found (404)'
    if 'BadRequestError' in names or status in (400, 413, 422):
        return f'invalid request ({status or 400})'
    if 'APIConnectionError' in names:
        return 'connection error'
    if isinstance(status, int) and status >= 500:
        return f'server unavailable or overloaded ({status})'
    return f'unexpected provider error ({type(error).__name__})'
