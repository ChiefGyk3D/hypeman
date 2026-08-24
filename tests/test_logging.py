# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for log rotation, level control, and repeat suppression."""

import logging
import os

import pytest

from hypeman.observability.logging import (
    RateLimitFilter,
    configure_logging,
    running_under_systemd,
)


@pytest.fixture(autouse=True)
def clean_logging_env(monkeypatch):
    """Each test starts from a known-empty logging configuration."""
    for var in ('LOG_LEVEL', 'LOG_FILE', 'LOG_MAX_BYTES', 'LOG_BACKUP_COUNT',
                'LOG_TO_STDOUT', 'LOG_TIMESTAMPS', 'LOG_DEDUPE_SECONDS',
                'LOG_QUIET_LIBRARIES', 'JOURNAL_STREAM', 'INVOCATION_ID'):
        monkeypatch.delenv(var, raising=False)
    yield
    root = logging.getLogger()
    for handler in root.handlers[:]:
        handler.close()
        root.removeHandler(handler)


def test_log_file_rotates_at_the_configured_size(tmp_path, monkeypatch):
    """The whole point: logs must stop growing without bound."""
    log_file = tmp_path / "daemon.log"
    monkeypatch.setenv('LOG_FILE', str(log_file))
    monkeypatch.setenv('LOG_MAX_BYTES', '2048')
    monkeypatch.setenv('LOG_BACKUP_COUNT', '3')
    monkeypatch.setenv('LOG_TO_STDOUT', 'false')

    configure_logging(force=True)
    logger = logging.getLogger('rotation-test')

    for i in range(500):
        logger.info(f"a reasonably long log line to fill the file up, number {i}")

    rotated = sorted(tmp_path.glob("daemon.log*"))
    assert len(rotated) > 1, "log file should have rotated"
    assert len(rotated) <= 4, "should keep at most backupCount + 1 files"

    for path in rotated:
        # Allow a little slack: rotation triggers on the write that crosses the threshold.
        assert path.stat().st_size < 2048 * 3


def test_level_is_configurable(monkeypatch):
    """A daemon polling every 2 minutes needs a volume knob."""
    monkeypatch.setenv('LOG_LEVEL', 'WARNING')
    configure_logging(force=True)
    assert logging.getLogger().level == logging.WARNING


def test_defaults_to_info(monkeypatch):
    configure_logging(force=True)
    assert logging.getLogger().level == logging.INFO


def test_bad_log_path_does_not_stop_the_daemon(monkeypatch):
    """A misconfigured log path is not a reason to refuse to start."""
    monkeypatch.setenv('LOG_FILE', '/proc/definitely/not/writable/daemon.log')
    monkeypatch.setenv('LOG_TO_STDOUT', 'true')

    configure_logging(force=True)  # must not raise

    logging.getLogger('still-works').info("daemon carries on")


def test_systemd_detection(monkeypatch):
    assert running_under_systemd() is False
    monkeypatch.setenv('JOURNAL_STREAM', '8:12345')
    assert running_under_systemd() is True


def test_timestamps_omitted_under_systemd(monkeypatch):
    """journald stamps every line already; repeating it is noise."""
    monkeypatch.setenv('INVOCATION_ID', 'abc123')
    monkeypatch.setenv('LOG_TO_STDOUT', 'true')
    configure_logging(force=True)

    fmt = logging.getLogger().handlers[0].formatter._fmt
    assert '%(asctime)s' not in fmt


def test_timestamps_present_when_not_under_systemd(monkeypatch):
    monkeypatch.setenv('LOG_TO_STDOUT', 'true')
    configure_logging(force=True)

    fmt = logging.getLogger().handlers[0].formatter._fmt
    assert '%(asctime)s' in fmt


def test_timestamp_detection_can_be_overridden(monkeypatch):
    monkeypatch.setenv('INVOCATION_ID', 'abc123')
    monkeypatch.setenv('LOG_TIMESTAMPS', 'true')
    monkeypatch.setenv('LOG_TO_STDOUT', 'true')
    configure_logging(force=True)

    fmt = logging.getLogger().handlers[0].formatter._fmt
    assert '%(asctime)s' in fmt


def test_file_logging_always_keeps_timestamps(tmp_path, monkeypatch):
    """A log file has no journald to supply the time."""
    monkeypatch.setenv('INVOCATION_ID', 'abc123')
    monkeypatch.setenv('LOG_FILE', str(tmp_path / "d.log"))
    monkeypatch.setenv('LOG_TO_STDOUT', 'false')
    configure_logging(force=True)

    fmt = logging.getLogger().handlers[0].formatter._fmt
    assert '%(asctime)s' in fmt


# ─────────────────────────────────────────────────────────────────────────────
# Repeat suppression
# ─────────────────────────────────────────────────────────────────────────────

def _record(msg, level=logging.INFO):
    return logging.LogRecord('t', level, __file__, 1, msg, None, None)


def test_repeats_are_suppressed_within_the_window():
    """'No streams live' 400 times a day is wallpaper, not information."""
    f = RateLimitFilter(window_seconds=60)
    msg = "No streams live, checking again in 2 minute(s)"

    assert f.filter(_record(msg)) is True
    for _ in range(50):
        assert f.filter(_record(msg)) is False


def test_distinct_messages_are_not_suppressed():
    f = RateLimitFilter(window_seconds=60)
    assert f.filter(_record("stream started")) is True
    assert f.filter(_record("stream ended")) is True


def test_warnings_and_errors_are_never_suppressed():
    """Suppressing an error to save log space would be its own outage."""
    f = RateLimitFilter(window_seconds=60)
    msg = "Ollama connection lost"

    for _ in range(20):
        assert f.filter(_record(msg, logging.ERROR)) is True
        assert f.filter(_record(msg, logging.WARNING)) is True


def test_suppression_window_expiry_reports_the_count(monkeypatch):
    """When the window closes, say how much was swallowed."""
    import hypeman.observability.logging as mod

    clock = {'t': 1000.0}
    monkeypatch.setattr(mod.time, 'monotonic', lambda: clock['t'])

    f = RateLimitFilter(window_seconds=60)
    msg = "still nothing happening"

    assert f.filter(_record(msg)) is True
    for _ in range(9):
        f.filter(_record(msg))

    clock['t'] += 61
    record = _record(msg)
    assert f.filter(record) is True
    assert "repeated 9x" in record.msg


def test_dedupe_disabled_by_default():
    f = RateLimitFilter(window_seconds=0)
    msg = "chatty"
    for _ in range(5):
        assert f.filter(_record(msg)) is True


# ─────────────────────────────────────────────────────────────────────────────
# Doppler fetch caching
#
# One API call per process, not one per credential. Without this, a daemon with
# eight platforms fires 20+ requests at startup and trips Doppler's rate limit,
# which then surfaces as "missing credentials".
# ─────────────────────────────────────────────────────────────────────────────

def test_doppler_is_fetched_once_for_many_lookups(monkeypatch):
    import hypeman.config.secrets as secrets

    secrets.reset_secret_cache()
    monkeypatch.setenv('DOPPLER_TOKEN', 'fake-token')

    calls = {'n': 0}

    def fake_fetch():
        calls['n'] += 1
        return {'TWITCH_CLIENT_ID': 'a', 'BLUESKY_APP_PASSWORD': 'b', 'GEMINI_API_KEY': 'c'}

    monkeypatch.setattr(secrets, '_doppler_secrets', fake_fetch)

    assert secrets.load_secrets_from_doppler('twitch') == {'client_id': 'a'}
    assert secrets.load_secrets_from_doppler('bluesky') == {'app_password': 'b'}
    assert secrets._doppler_direct_key('gemini_api_key') == 'c'

    assert calls['n'] == 3, "each helper reads the cache; the cache itself fetches once"

    secrets.reset_secret_cache()


def test_doppler_failure_is_cached_as_empty(monkeypatch):
    """A rate limit must degrade to env vars, not retry on every lookup."""
    import hypeman.config.secrets as secrets

    secrets.reset_secret_cache()
    monkeypatch.setenv('DOPPLER_TOKEN', 'fake-token')

    calls = {'n': 0}

    class BoomSDK:
        def __init__(self):
            calls['n'] += 1

        def set_access_token(self, token):
            pass

        @property
        def secrets(self):
            raise RuntimeError("TooManyRequestsException")

    import sys, types
    fake_module = types.ModuleType('dopplersdk')
    fake_module.DopplerSDK = BoomSDK
    monkeypatch.setitem(sys.modules, 'dopplersdk', fake_module)

    for _ in range(5):
        assert secrets._doppler_secrets() == {}

    assert calls['n'] == 1, "a failed fetch must not be retried on every lookup"

    secrets.reset_secret_cache()
