# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Regression tests for the outage that started all this.

The original daemons treated "is the AI server up?" as a boot-time constant.
Take the server offline and the daemon kept running, silently posting template
fallbacks, and only a restart brought AI generation back.

There were two separate versions of the bug:

  1. Boon-Tube-Daemon had no reconnect logic at all, and set `self.llm = None`
     when startup authentication failed. Unrecoverable by construction.

  2. stream-daemon HAD reconnect logic, but every call site gated on
     `ai_generator.enabled`, and the reconnect lived inside a method that gate
     prevented from ever being called. Dead code exactly when it was needed.

These tests fail against either of those designs.
"""

import time

import pytest

from hypeman_social.llm.base import BaseLLM
from hypeman_social.llm.profiles import GENERIC_PROFILE


class FakeLLM(BaseLLM):
    """
    A provider backed by a switch, standing in for a local AI box.

    Flip `server_up` to simulate the flood taking the machine offline and the
    machine coming back.
    """

    provider_name = 'fake'

    def __init__(self, server_up=True, **kwargs):
        super().__init__(profile=GENERIC_PROFILE, **kwargs)
        self.server_up = server_up
        self.reconnect_calls = 0
        self.generate_calls = 0
        # No cooldown by default so tests don't sleep.
        self.reconnect_interval = 0

    def authenticate(self):
        # Config is read before the connection is attempted, so a failure here
        # still leaves us able to reconnect later. This is the crucial bit.
        self._configured = True
        if not self.server_up:
            return False
        self.enabled = True
        self._connection_was_successful = True
        return True

    def _reconnect(self):
        self.reconnect_calls += 1
        return self.server_up

    def _raw_generate(self, prompt, max_tokens):
        self.generate_calls += 1
        if not self.server_up:
            raise ConnectionError("connection refused")
        return "a generated message"


# ─────────────────────────────────────────────────────────────────────────────
# Bug 1: a failed startup must not be permanent
# ─────────────────────────────────────────────────────────────────────────────

def test_failed_startup_is_not_permanent():
    """A daemon started while the AI server is down must recover without a restart."""
    llm = FakeLLM(server_up=False)

    assert llm.authenticate() is False
    assert llm.is_available() is False

    # The server comes back. Nothing restarts the daemon.
    llm.server_up = True

    assert llm.is_available() is True, "must recover after a failed initial authenticate"
    assert llm.generate("prompt") == "a generated message"


def test_failed_startup_retains_configuration():
    """Recovery is only possible because config survives a failed connection."""
    llm = FakeLLM(server_up=False)
    llm.authenticate()

    assert llm._configured is True, "config must be retained so reconnect has a target"


def test_unconfigured_provider_never_attempts_recovery():
    """No config means nothing to reconnect to — don't spin on it."""
    llm = FakeLLM(server_up=True)
    # Never authenticated, so never configured.
    assert llm.is_available() is False
    assert llm.reconnect_calls == 0


# ─────────────────────────────────────────────────────────────────────────────
# Bug 2: the availability check itself must be able to heal
# ─────────────────────────────────────────────────────────────────────────────

def test_is_available_recovers_a_downed_provider():
    """
    The gate callers use must be the thing that heals.

    stream-daemon's bug: callers checked `.enabled`, which could only ever go
    False->stay False, so the reconnect behind it was unreachable.
    """
    llm = FakeLLM(server_up=True)
    llm.authenticate()

    llm.server_up = False
    llm.mark_unavailable("server went away")
    assert llm.enabled is False

    llm.server_up = True

    assert llm.is_available() is True
    assert llm.reconnect_calls >= 1
    assert llm.enabled is True


def test_generation_failure_recovers_on_next_call():
    """A mid-flight outage recovers by itself once the server returns."""
    llm = FakeLLM(server_up=True)
    llm.authenticate()

    llm.server_up = False
    assert llm.generate("prompt") is None
    assert llm.enabled is False

    llm.server_up = True
    assert llm.generate("prompt") == "a generated message"


def test_still_down_reports_unavailable():
    """Recovery attempts on a genuinely dead server must fail honestly."""
    llm = FakeLLM(server_up=True)
    llm.authenticate()

    llm.server_up = False
    llm.mark_unavailable()

    assert llm.is_available() is False
    assert llm.generate("prompt") is None


# ─────────────────────────────────────────────────────────────────────────────
# Reconnect pacing
# ─────────────────────────────────────────────────────────────────────────────

def test_cooldown_prevents_hammering_a_down_server():
    """Don't retry a dead server on every single poll."""
    llm = FakeLLM(server_up=True)
    llm.authenticate()
    llm.reconnect_interval = 3600

    llm.server_up = False
    llm.mark_unavailable()

    for _ in range(10):
        llm.is_available()

    assert llm.reconnect_calls <= 1, "cooldown must suppress repeated reconnects"


def test_cooldown_expiry_allows_another_attempt():
    """Once the cooldown passes, try again."""
    llm = FakeLLM(server_up=True)
    llm.authenticate()
    llm.reconnect_interval = 1

    llm.server_up = False
    llm.mark_unavailable()
    llm.is_available()
    first = llm.reconnect_calls

    time.sleep(1.05)
    llm.server_up = True

    assert llm.is_available() is True
    assert llm.reconnect_calls > first


def test_auto_reconnect_can_be_disabled():
    """Operators who want strict fail-fast behaviour can have it."""
    llm = FakeLLM(server_up=True)
    llm.authenticate()
    llm.enable_auto_reconnect = False

    llm.mark_unavailable()
    llm.server_up = True

    assert llm.is_available() is False
    assert llm.reconnect_calls == 0


def test_max_reconnect_attempts_is_respected():
    """A bounded retry budget stops after the configured number of tries."""
    llm = FakeLLM(server_up=False)
    llm.authenticate()
    llm.max_reconnect_attempts = 3

    for _ in range(10):
        llm.is_available()

    assert llm.reconnect_calls == 3


def test_successful_reconnect_resets_the_attempt_counter():
    """A flap shouldn't permanently consume the retry budget."""
    llm = FakeLLM(server_up=False)
    llm.authenticate()
    llm.max_reconnect_attempts = 3

    llm.is_available()
    assert llm._reconnect_attempt_count == 1

    llm.server_up = True
    assert llm.is_available() is True
    assert llm._reconnect_attempt_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# Error classification
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "connection refused",
    "Failed to establish a new connection",
    "host unreachable",
    "no route to host",
])
def test_connection_errors_are_recognised(message):
    """Transport failures must trigger recovery, not a silent give-up."""
    llm = FakeLLM()
    assert llm._is_connection_error(Exception(message)) is True


@pytest.mark.parametrize("message", ["read timed out", "request timeout"])
def test_timeouts_are_not_connection_failures(message):
    """
    A slow response means the server is up and struggling.

    Classifying it as unreachable would mark a healthy provider down over one
    slow request, and for a cloud provider like Gemini that is simply wrong.
    """
    llm = FakeLLM()
    assert llm._is_connection_error(Exception(message)) is False


@pytest.mark.parametrize("message", [
    "model not found",
    "invalid request",
    "unauthorized",
])
def test_permanent_errors_are_recognised(message):
    """Retrying a bad model name forever helps nobody."""
    llm = FakeLLM()
    assert llm._is_permanent_error(Exception(message)) is True


def test_permanent_error_does_not_mark_provider_down():
    """A bad prompt shouldn't convince us the server died."""
    class BadRequestLLM(FakeLLM):
        def _raw_generate(self, prompt, max_tokens):
            raise ValueError("invalid request")

    llm = BadRequestLLM(server_up=True)
    llm.authenticate()

    assert llm.generate("prompt") is None
    assert llm.enabled is True, "a permanent request error must not disable the provider"


def test_status_reports_provider_state():
    """Health reporting needs to see what's actually going on."""
    llm = FakeLLM(server_up=False)
    llm.authenticate()

    status = llm.status()
    assert status['provider'] == 'fake'
    assert status['enabled'] is False
    assert status['configured'] is True
    assert status['ever_connected'] is False


# ─────────────────────────────────────────────────────────────────────────────
# Active liveness probing
#
# is_available() is optimistic — while it believes the provider is up it
# returns True without touching the network, and only notices an outage when a
# generation fails. heartbeat() is the counterpart a poll loop calls so that
# /status stays honest and the first post-outage announcement isn't needlessly
# a template.
# ─────────────────────────────────────────────────────────────────────────────

def test_is_available_is_optimistic_while_it_believes_it_is_up():
    """Documents the design: no network round-trip on the hot path."""
    llm = FakeLLM(server_up=True)
    llm.authenticate()

    llm.server_up = False  # server dies; nothing has told the provider yet

    assert llm.is_available() is True
    assert llm.reconnect_calls == 0


def test_heartbeat_detects_a_silent_outage():
    """The probe is what turns 'believed up' into 'actually up'."""
    llm = FakeLLM(server_up=True)
    llm.authenticate()

    llm.server_up = False
    assert llm.heartbeat(min_interval=0) is False
    assert llm.enabled is False


def test_heartbeat_detects_recovery_without_a_generation():
    """Recovery is noticed on the poll loop's schedule, not the next stream."""
    llm = FakeLLM(server_up=True)
    llm.authenticate()

    llm.server_up = False
    llm.heartbeat(min_interval=0)
    assert llm.enabled is False

    llm.server_up = True
    assert llm.heartbeat(min_interval=0) is True
    assert llm.enabled is True


def test_heartbeat_is_rate_limited():
    """Safe to call every poll cycle without hammering the server."""
    llm = FakeLLM(server_up=True)
    llm.authenticate()
    llm.reconnect_interval = 3600

    for _ in range(10):
        llm.heartbeat()

    assert llm.reconnect_calls <= 1


def test_probe_resets_the_reconnect_budget_on_success():
    llm = FakeLLM(server_up=False)
    llm.authenticate()
    llm.heartbeat(min_interval=0)
    assert llm._reconnect_attempt_count >= 0

    llm.server_up = True
    llm.probe()
    assert llm._reconnect_attempt_count == 0
    assert llm._last_error is None


def test_probe_on_unconfigured_provider_is_a_no_op():
    llm = FakeLLM(server_up=True)
    assert llm.probe() is False
