# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Base class for LLM providers.

The important thing in this file is the availability contract. The original
daemons tracked liveness in a plain `enabled` boolean that was set once at
startup and, on failure, never set again — so a local AI server going down for
five minutes meant no AI until somebody noticed and restarted the process.

Here, `is_available()` is the only thing callers should ever check, and it is
allowed to heal: if the provider is down but recoverable, it attempts a
cooldown-guarded reconnect and reports the result. Never branch on `.enabled`
from outside this package. That's the bug.
"""

import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from hypeman.config import get_bool_config, get_config
from hypeman.llm import guardrails
from hypeman.llm.profiles import ContentProfile, GENERIC_PROFILE

logger = logging.getLogger(__name__)


class BaseLLM(ABC):
    """
    Shared behaviour for every LLM provider: config, guardrails, dedup, recovery.

    Subclasses implement three things:
        authenticate()   — establish a connection, return True on success
        _raw_generate()  — one generation call, no retries, raise on failure
        provider_name    — a short label for logs and health output
    """

    provider_name = 'base'

    #: Errors matching these substrings mean the server is unreachable rather
    #: than the request being bad. Only these trigger a reconnect attempt.
    #: Note that timeouts are deliberately NOT here. A slow response means the
    #: server is up and struggling, which is worth retrying; treating it as
    #: "unreachable" would mark a healthy provider down over one slow request.
    CONNECTION_ERROR_MARKERS = (
        'connection', 'connect', 'refused', 'unreachable',
        'failed to establish', 'no route',
    )

    #: Transient conditions worth retrying with backoff.
    RETRYABLE_ERROR_MARKERS = (
        '503', '429', 'overloaded', 'quota', 'timeout', 'timed out',
        'unavailable', 'rate limit',
    )

    #: Errors matching these are permanent — retrying just wastes time.
    PERMANENT_ERROR_MARKERS = ('not found', 'invalid', 'unauthorized', 'forbidden')

    def __init__(self, profile: ContentProfile = GENERIC_PROFILE):
        self.profile = profile

        # Liveness. Read this through is_available(), never directly.
        self.enabled = False

        # True once we have enough config to *try* connecting. This is what
        # makes recovery possible after a failed initial authenticate(): we may
        # never have connected, but we still know where the server should be.
        self._configured = False

        # Recovery state.
        self.enable_auto_reconnect = get_bool_config('LLM', 'enable_auto_reconnect', default=True)
        self.reconnect_interval = int(get_config('LLM', 'reconnect_interval', default='60'))
        self.max_reconnect_attempts = int(get_config('LLM', 'max_reconnect_attempts', default='0'))
        self._last_reconnect_attempt = 0.0
        self._last_probe = 0.0
        self._reconnect_attempt_count = 0
        self._connection_was_successful = False
        self._last_error: Optional[str] = None

        # Generation parameters. Low temperature because small models get
        # "creative" and start inventing giveaways.
        self.temperature = float(get_config('LLM', 'temperature', default='0.3'))
        self.top_p = float(get_config('LLM', 'top_p', default='0.9'))
        self.max_tokens = int(get_config('LLM', 'max_tokens', default='150'))
        self.max_retries = int(get_config('LLM', 'max_retries', default='3'))
        self.retry_delay_base = int(get_config('LLM', 'retry_delay_base', default='2'))

        # Reasoning-model support (Qwen3 and friends).
        self.enable_thinking_mode = get_bool_config('LLM', 'enable_thinking_mode', default=False)
        self.thinking_token_multiplier = float(
            get_config('LLM', 'thinking_token_multiplier', default='4.0')
        )

        # Guardrail toggles.
        self.enable_deduplication = get_bool_config('LLM', 'enable_deduplication', default=True)
        self.dedup_cache_size = int(get_config('LLM', 'dedup_cache_size', default='20'))
        self.enable_quality_scoring = get_bool_config('LLM', 'enable_quality_scoring', default=False)
        self.min_quality_score = int(get_config('LLM', 'min_quality_score', default='6'))
        self.max_emoji_count = int(get_config('LLM', 'max_emoji_count', default='2'))
        self.enable_profanity_filter = get_bool_config('LLM', 'enable_profanity_filter', default=False)
        self.profanity_severity = get_config('LLM', 'profanity_severity', default='moderate')
        self.enable_platform_validation = get_bool_config('LLM', 'enable_platform_validation', default=True)

        # Recently posted messages, so we don't repeat ourselves.
        self._message_cache: List[str] = []

    # ─────────────────────────────────────────────────────────────────────
    # Availability — the whole point of this class
    # ─────────────────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """
        Can we generate right now?

        This is the ONLY liveness check callers should use. If the provider is
        currently down but recoverable, this attempts a reconnect (respecting
        the cooldown) and reports whether it worked.

        Returns:
            True if a generation call has a reasonable chance of succeeding.
        """
        if self.enabled:
            return True

        if not self._configured:
            # Never had usable config — nothing to reconnect to.
            return False

        return self._attempt_recovery()

    def _attempt_recovery(self) -> bool:
        """
        Try to restore a downed connection, respecting the cooldown.

        Subclasses supply the actual reconnect via _reconnect(). Providers that
        can't recover (or shouldn't) leave _reconnect() returning False.

        Returns:
            True if the provider is usable again.
        """
        if not self.enable_auto_reconnect:
            return False

        if 0 < self.max_reconnect_attempts <= self._reconnect_attempt_count:
            logger.debug(
                f"{self.provider_name}: max reconnect attempts "
                f"({self.max_reconnect_attempts}) reached, staying down"
            )
            return False

        # Don't hammer a server that's genuinely down.
        elapsed = time.time() - self._last_reconnect_attempt
        if elapsed < self.reconnect_interval:
            logger.debug(
                f"{self.provider_name}: reconnect cooldown, "
                f"{self.reconnect_interval - elapsed:.0f}s remaining"
            )
            return False

        self._last_reconnect_attempt = time.time()
        self._reconnect_attempt_count += 1

        logger.info(
            f"🔄 Attempting {self.provider_name} reconnect "
            f"(attempt #{self._reconnect_attempt_count})..."
        )

        try:
            if self._reconnect():
                self.enabled = True
                self._connection_was_successful = True
                self._reconnect_attempt_count = 0
                self._last_error = None
                logger.info(f"✓ {self.provider_name} reconnected")
                return True
        except Exception as e:
            self._last_error = f"{type(e).__name__}: {e}"

        logger.warning(
            f"⚠ {self.provider_name} reconnect failed "
            f"(attempt #{self._reconnect_attempt_count})"
        )
        return False

    def _reconnect(self) -> bool:
        """Re-establish the provider connection. Override in subclasses."""
        return False

    def probe(self) -> bool:
        """
        Actively verify the provider still answers, and update state.

        is_available() is deliberately optimistic: while it believes the
        provider is up it returns True without a network round-trip, and only
        notices an outage when a generation fails. That is cheap and right for
        the hot path, but it means two things go stale:

          * /status can report the AI as healthy while the server is down
          * the first announcement after an outage falls back to a template
            even if the server has already come back

        A poll loop should call heartbeat() once a cycle so neither happens.

        Returns:
            True if the provider answered.
        """
        if not self._configured:
            return False

        try:
            answered = self._reconnect()
        except Exception as e:
            self._last_error = f"{type(e).__name__}: {e}"
            answered = False

        if answered:
            was_down = not self.enabled
            self.enabled = True
            self._connection_was_successful = True
            self._reconnect_attempt_count = 0
            self._last_error = None
            if was_down:
                logger.info(f"✓ {self.provider_name} is back (found by heartbeat)")
        else:
            if self.enabled:
                logger.warning(f"⚠ {self.provider_name} stopped answering (found by heartbeat)")
            self.mark_unavailable(self._last_error or 'probe failed')

        return answered

    def heartbeat(self, min_interval: Optional[int] = None) -> bool:
        """
        Rate-limited probe, safe to call every poll cycle.

        Args:
            min_interval: Seconds between probes. Defaults to reconnect_interval.

        Returns:
            Current availability.
        """
        interval = self.reconnect_interval if min_interval is None else min_interval

        elapsed = time.time() - self._last_probe
        if elapsed < interval:
            return self.enabled

        self._last_probe = time.time()
        return self.probe()

    def mark_unavailable(self, error: Optional[str] = None) -> None:
        """
        Record that the provider just went down.

        Safe to call repeatedly. Deliberately does NOT reset the reconnect
        counter, so a flapping server still respects max_reconnect_attempts.
        """
        self.enabled = False
        if error:
            self._last_error = error

    def provider_config(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """
        Read a setting, preferring a provider-specific key over the shared one.

        Looks up LLM_<PROVIDER>_<KEY> first, then falls back to LLM_<KEY>:

            LLM_OLLAMA_MODEL=gemma3:12b     <- used when provider is ollama
            LLM_GEMINI_MODEL=gemini-2.0-flash-lite
            LLM_MODEL=...                   <- shared fallback, still honoured

        This exists because a single LLM_MODEL cannot serve a primary and a
        fallback at once: pointing Ollama at 'gemini-2.0-flash-lite' asks a
        local server for a model it has never heard of, and every generation
        quietly degrades to a template.

        Args:
            key: Setting name without the LLM_ prefix, e.g. 'model'.
            default: Value when neither key is set.

        Returns:
            The configured value, or the default.
        """
        specific = get_config('LLM', f'{self.provider_name}_{key}')
        if specific:
            return specific
        return get_config('LLM', key, default=default)

    def _is_connection_error(self, error: Exception) -> bool:
        """True if this exception means "server unreachable" rather than "bad request"."""
        text = str(error).lower()
        return any(marker in text for marker in self.CONNECTION_ERROR_MARKERS)

    def _is_permanent_error(self, error: Exception) -> bool:
        """True if retrying this exception is pointless."""
        text = str(error).lower()
        return any(marker in text for marker in self.PERMANENT_ERROR_MARKERS)

    def status(self) -> Dict[str, Any]:
        """Machine-readable provider state, for the health endpoint."""
        return {
            'provider': self.provider_name,
            'enabled': self.enabled,
            'configured': self._configured,
            'ever_connected': self._connection_was_successful,
            'reconnect_attempts': self._reconnect_attempt_count,
            'last_probe_age_seconds': (
                int(time.time() - self._last_probe) if self._last_probe else None
            ),
            'last_error': self._last_error,
        }

    # ─────────────────────────────────────────────────────────────────────
    # Generation
    # ─────────────────────────────────────────────────────────────────────

    @abstractmethod
    def authenticate(self) -> bool:
        """Establish the initial connection. Must set _configured once config is read."""

    @abstractmethod
    def _raw_generate(self, prompt: str, max_tokens: int) -> Optional[str]:
        """Perform one generation call. Raise on failure; don't retry in here."""

    def generate(
        self,
        prompt: str,
        max_retries: Optional[int] = None,
        max_tokens: Optional[int] = None,
    ) -> Optional[str]:
        """
        Generate text, with retries, backoff, and automatic reconnection.

        Args:
            prompt: The prompt to send.
            max_retries: Attempts before giving up. Defaults to config.
            max_tokens: Response token cap. Defaults to config.

        Returns:
            The generated text, or None if generation failed.
        """
        # Recover first if we're down. This is what the old code couldn't do,
        # because callers gated on `.enabled` and never got this far.
        if not self.is_available():
            logger.debug(f"{self.provider_name} unavailable, skipping generation")
            return None

        max_retries = self.max_retries if max_retries is None else max_retries
        max_tokens = self.max_tokens if max_tokens is None else max_tokens

        # Reasoning models need headroom to think before they answer.
        effective_tokens = max_tokens
        if self.enable_thinking_mode:
            effective_tokens = int(max_tokens * self.thinking_token_multiplier)

        delay = float(self.retry_delay_base)

        for attempt in range(max_retries):
            try:
                result = self._raw_generate(prompt, effective_tokens)
                if result:
                    return result
                logger.warning(f"Empty response from {self.provider_name}")
                return None

            except Exception as e:
                if self._is_permanent_error(e):
                    logger.error(f"✗ {self.provider_name} permanent error: {type(e).__name__}")
                    return None

                if self._is_connection_error(e):
                    logger.error(f"✗ {self.provider_name} connection lost: {type(e).__name__}")
                    self.mark_unavailable(f"{type(e).__name__}: {e}")

                    # Try to come straight back up; if the server is genuinely
                    # gone, is_available() will retry on the next call instead.
                    if self._attempt_recovery():
                        logger.info(f"🔄 Retrying generation after {self.provider_name} reconnect")
                        continue
                    return None

                if attempt < max_retries - 1:
                    logger.warning(
                        f"⚠ {self.provider_name} error "
                        f"(attempt {attempt + 1}/{max_retries}), retrying in {delay:.0f}s"
                    )
                    time.sleep(delay)
                    delay *= 2
                else:
                    logger.error(
                        f"✗ {self.provider_name} failed after {max_retries} attempts: "
                        f"{type(e).__name__}"
                    )

        return None

    # ─────────────────────────────────────────────────────────────────────
    # Deduplication (stateful, so it lives here rather than in guardrails)
    # ─────────────────────────────────────────────────────────────────────

    def is_duplicate_message(self, message: str) -> bool:
        """True if this message is too close to something we posted recently."""
        if not self.enable_deduplication or not self._message_cache:
            return False

        normalized = self._normalize_for_dedup(message)

        for cached in self._message_cache:
            cached_norm = self._normalize_for_dedup(cached)
            if normalized == cached_norm:
                return True

            # Heavy word overlap counts as a repeat even if phrasing shifted.
            words = set(normalized.split())
            cached_words = set(cached_norm.split())
            if words and cached_words:
                overlap = len(words & cached_words) / max(len(words), len(cached_words))
                if overlap > 0.8:
                    return True

        return False

    def add_to_message_cache(self, message: str) -> None:
        """Remember a message so we don't post something near-identical next time."""
        if not self.enable_deduplication:
            return

        self._message_cache.append(message)
        if len(self._message_cache) > self.dedup_cache_size:
            self._message_cache = self._message_cache[-self.dedup_cache_size:]

    @staticmethod
    def _normalize_for_dedup(message: str) -> str:
        """Strip hashtags, punctuation and case so near-misses compare equal."""
        import re
        text = re.sub(r'#\w+', '', message)
        text = re.sub(r'[^\w\s]', '', text)
        return ' '.join(text.lower().split())

    # ─────────────────────────────────────────────────────────────────────
    # Guardrails
    # ─────────────────────────────────────────────────────────────────────

    def apply_guardrails(
        self,
        message: str,
        title: str,
        username: str,
        platform: str,
        char_limit: int,
        expected_hashtag_count: int = 0,
    ) -> Tuple[Optional[str], List[str]]:
        """
        Run a generated message through every enabled quality check.

        Args:
            message: The raw generated message.
            title: Source title, for relevance checks.
            username: Account name, used to strip self-referential hashtags.
            platform: Target social platform.
            char_limit: Platform character limit.
            expected_hashtag_count: Exactly how many hashtags are expected.

        Returns:
            (message, issues). The message is None if it failed a hard check and
            should not be posted; issues always explains what was found.
        """
        issues: List[str] = []

        if not message:
            return None, ['Empty message']

        message = message.strip().strip('"').strip()

        # Strip hashtags derived from the account's own name — nobody needs
        # #ChiefGyk3D on a post from ChiefGyk3D.
        if username:
            message = guardrails.validate_hashtags_against_username(message, username)

        # Hard validation: fabricated details, wrong hashtag count, inline URLs.
        is_valid, validation_issues = guardrails.validate_message_quality(
            message, expected_hashtag_count, title, username, self.profile
        )
        if not is_valid:
            issues.extend(validation_issues)

        if self.enable_platform_validation:
            issues.extend(guardrails.validate_platform_specific(message, platform))

        # Too many emoji reads as spam.
        emoji_count = guardrails.count_emojis(message)
        if emoji_count > self.max_emoji_count:
            issues.append(f'Too many emoji: {emoji_count} (max {self.max_emoji_count})')

        if self.enable_profanity_filter:
            has_profanity, found = guardrails.contains_profanity(message, self.profanity_severity)
            if has_profanity:
                issues.append(f"Contains profanity: {', '.join(found)}")

        if self.enable_quality_scoring:
            score, score_issues = guardrails.score_message_quality(message, title, self.profile)
            if score < self.min_quality_score:
                issues.append(f'Quality score {score} below minimum {self.min_quality_score}')
                issues.extend(score_issues)

        if self.is_duplicate_message(message):
            issues.append('Duplicate of a recently posted message')

        if issues:
            return None, issues

        message = guardrails.safe_trim(message, char_limit)
        self.add_to_message_cache(message)
        return message, []
