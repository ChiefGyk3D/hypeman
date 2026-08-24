# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Logging setup that behaves in every place these daemons actually run.

The daemons previously called logging.basicConfig() with a fixed INFO level and
no file handler. That's fine for an afternoon and miserable for a year: a
stream-daemon polling every two minutes writes ~720 status blocks a day, at a
volume you cannot turn down, to a destination you cannot rotate from inside the
app.

This module gives you:

  * A configurable level, so routine polling can drop to DEBUG and stay out of
    the way while real events remain visible at INFO.
  * Optional file logging with size-based rotation, for people running the
    daemon by hand or under a supervisor that doesn't rotate for them.
  * journald-aware formatting — under systemd, journald already stamps every
    line with a timestamp, so repeating it in the message is pure noise.
  * A rate-limiting filter for the repetitive "still nothing happening" lines.

Everything is opt-in via env vars, so an existing deployment that sets nothing
gets the same behaviour it has today, minus the parts that were annoying.
"""

import logging
import logging.handlers
import os
import sys
import time
from typing import Optional

from hypeman.config import get_bool_config, get_config, get_int_config

DEFAULT_FORMAT = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
JOURNALD_FORMAT = '[%(levelname)s] %(name)s: %(message)s'
DATE_FORMAT = '%Y-%m-%d %H:%M:%S'


def running_under_systemd() -> bool:
    """
    Detect whether we were started by systemd.

    systemd sets JOURNAL_STREAM for services whose output goes to the journal.
    INVOCATION_ID is set for any unit invocation, which covers the case where
    output has been redirected elsewhere.
    """
    return bool(os.getenv('JOURNAL_STREAM') or os.getenv('INVOCATION_ID'))


class RateLimitFilter(logging.Filter):
    """
    Collapse identical log lines that repeat within a time window.

    Aimed squarely at the poll loop: "No streams live, checking again in 2
    minute(s)" is useful the first time and wallpaper by the four hundredth.
    The first occurrence passes through immediately; repeats are suppressed
    until the window expires, at which point one line reports how many were
    swallowed.

    Only applies to records at or below the configured level, so warnings and
    errors are never suppressed.
    """

    def __init__(self, window_seconds: int = 300, max_level: int = logging.INFO):
        super().__init__()
        self.window = window_seconds
        self.max_level = max_level
        self._seen = {}  # message -> [last_emitted, suppressed_count]

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno > self.max_level or self.window <= 0:
            return True

        try:
            key = record.getMessage()
        except Exception:
            return True

        now = time.monotonic()
        entry = self._seen.get(key)

        if entry is None:
            self._seen[key] = [now, 0]
            return True

        last, suppressed = entry

        if now - last < self.window:
            entry[1] = suppressed + 1
            return False

        # Window expired. Let this one through, noting what we swallowed.
        if suppressed:
            record.msg = f"{key}  (repeated {suppressed}x in the last {self.window}s)"
            record.args = ()
        self._seen[key] = [now, 0]
        return True


def configure_logging(
    level: Optional[str] = None,
    log_file: Optional[str] = None,
    force: bool = False,
) -> logging.Logger:
    """
    Configure logging for a daemon. Call once, early, from your entry point.

    Configuration, all optional:

        LOG_LEVEL             DEBUG|INFO|WARNING|ERROR       (default INFO)
        LOG_FILE              path; enables rotating file output (default none)
        LOG_MAX_BYTES         rotate at this size             (default 10485760 = 10MB)
        LOG_BACKUP_COUNT      how many rotated files to keep  (default 5)
        LOG_TO_STDOUT         also log to stdout              (default true)
        LOG_TIMESTAMPS        include timestamps in messages  (default: auto)
        LOG_DEDUPE_SECONDS    collapse repeats within N secs  (default 0 = off)
        LOG_QUIET_LIBRARIES   quiet chatty HTTP libraries     (default true)

    Args:
        level: Overrides LOG_LEVEL.
        log_file: Overrides LOG_FILE.
        force: Replace existing handlers. Useful when a library already called
            basicConfig() behind your back.

    Returns:
        The configured root logger.
    """
    level_name = (level or get_config('Log', 'level', default='INFO') or 'INFO').upper()
    resolved_level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    if force:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
    root.setLevel(resolved_level)

    # Timestamps: on by default, but off under systemd where journald already
    # adds them. Set LOG_TIMESTAMPS explicitly to override the detection.
    timestamps_cfg = get_config('Log', 'timestamps', default=None)
    if timestamps_cfg is None:
        include_timestamps = not running_under_systemd()
    else:
        include_timestamps = str(timestamps_cfg).strip().lower() in ('true', '1', 'yes', 'on')

    fmt = DEFAULT_FORMAT if include_timestamps else JOURNALD_FORMAT

    # Log in local time rather than UTC — matches what both daemons did before.
    logging.Formatter.converter = time.localtime
    formatter = logging.Formatter(fmt, datefmt=DATE_FORMAT)

    dedupe_seconds = get_int_config('Log', 'dedupe_seconds', default=0)
    rate_filter = RateLimitFilter(dedupe_seconds) if dedupe_seconds > 0 else None

    handlers = []

    if get_bool_config('Log', 'to_stdout', default=True):
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        handlers.append(stream_handler)

    # Rotating file output. This is the piece that was missing entirely: with
    # no file handler at all, "rotation" was whatever journald or Docker
    # happened to do, and nothing at all when run by hand in a terminal.
    resolved_file = log_file or get_config('Log', 'file', default=None)
    if resolved_file:
        try:
            directory = os.path.dirname(os.path.abspath(resolved_file))
            if directory:
                os.makedirs(directory, exist_ok=True)

            file_handler = logging.handlers.RotatingFileHandler(
                resolved_file,
                maxBytes=get_int_config('Log', 'max_bytes', default=10 * 1024 * 1024),
                backupCount=get_int_config('Log', 'backup_count', default=5),
                encoding='utf-8',
            )
            # Files always get timestamps; there's no journald to add them.
            file_handler.setFormatter(logging.Formatter(DEFAULT_FORMAT, datefmt=DATE_FORMAT))
            handlers.append(file_handler)
        except OSError as e:
            # A bad log path must not stop the daemon from starting.
            print(
                f"⚠ Could not open log file {resolved_file}: {type(e).__name__}: {e}",
                file=sys.stderr,
            )

    for handler in handlers:
        if rate_filter is not None:
            handler.addFilter(rate_filter)
        root.addHandler(handler)

    # These libraries log every HTTP request at INFO, which triples log volume
    # for no benefit once you know the daemon works.
    if get_bool_config('Log', 'quiet_libraries', default=True):
        for noisy in ('urllib3', 'httpx', 'httpcore', 'requests', 'atproto',
                      'mastodon', 'googleapiclient', 'google', 'boto3', 'botocore'):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    destinations = []
    if get_bool_config('Log', 'to_stdout', default=True):
        destinations.append('stdout')
    if resolved_file:
        destinations.append(resolved_file)
    logger.debug(
        f"Logging configured: level={level_name}, "
        f"destinations={', '.join(destinations) or 'none'}, "
        f"timestamps={include_timestamps}, dedupe={dedupe_seconds}s"
    )

    return root
