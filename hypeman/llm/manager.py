# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
LLMManager — one handle for "generate me a post", with automatic failover.

Both daemons already shipped support for Ollama *and* Gemini, but the choice
was static: pick one at boot and live with it. That's a shame, because the two
have complementary failure modes. Your local Ollama box goes down for a flood,
a power cut, or a GPU driver update. Gemini goes down for rate limits and
outages. They rarely go down together.

So: configure a primary and a fallback. When the primary is unavailable, the
fallback covers, and the manager keeps checking whether the primary is back —
because coming home to find you're still paying Google two weeks after the
local box recovered would be its own kind of annoying.

Fallback is opt-in. If you chose Ollama specifically so your data stays on your
network, silently shipping prompts to Google would be a betrayal, not a feature.
Set LLM_FALLBACK_PROVIDER to enable it.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from hypeman.config import get_bool_config, get_config
from hypeman.llm.base import BaseLLM
from hypeman.llm.profiles import ContentProfile, GENERIC_PROFILE

logger = logging.getLogger(__name__)


def _normalize_for_dedup(message: str) -> str:
    """Strip hashtags, punctuation and case so near-misses compare equal."""
    import re
    text = re.sub(r'#\w+', '', message)
    text = re.sub(r'[^\w\s]', '', text)
    return ' '.join(text.lower().split())


def _build_provider(name: str, profile: ContentProfile) -> Optional[BaseLLM]:
    """Instantiate a provider by name. Returns None for an unknown name."""
    name = (name or '').strip().lower()

    if name == 'ollama':
        from hypeman.llm.ollama import OllamaLLM
        return OllamaLLM(profile=profile)

    if name == 'gemini':
        from hypeman.llm.gemini import GeminiLLM
        return GeminiLLM(profile=profile)

    if name in ('', 'none'):
        return None

    logger.error(f"✗ Unknown LLM provider: {name}")
    return None


class LLMManager:
    """
    Wraps a primary provider and an optional fallback.

    Exposes the same availability contract as BaseLLM, so daemons only ever
    call is_available() and generate(). Which provider actually served a
    request is an implementation detail — visible in status(), not in the
    call site.
    """

    def __init__(self, profile: ContentProfile = GENERIC_PROFILE):
        self.profile = profile
        self.primary: Optional[BaseLLM] = None
        self.fallback: Optional[BaseLLM] = None
        self.enabled = False

        #: Set while the fallback is covering for a downed primary.
        self.using_fallback = False

        # Deduplication lives here rather than on a provider — see
        # is_duplicate_message() for why.
        self.enable_deduplication = get_bool_config('LLM', 'enable_deduplication', default=True)
        self.dedup_cache_size = int(get_config('LLM', 'dedup_cache_size', default='20'))
        self._message_cache: List[str] = []

    def authenticate(self) -> bool:
        """
        Bring up the configured providers.

        Returns True if at least one provider is usable. A primary that fails
        to connect is NOT fatal — it stays configured and keeps trying in the
        background, which is the entire point of this rewrite.
        """
        if not get_bool_config('LLM', 'enable', default=False):
            logger.info("⊘ LLM generation disabled")
            return False

        primary_name = get_config('LLM', 'provider', default='gemini')
        self.primary = _build_provider(primary_name, self.profile)

        if self.primary is None:
            logger.error(f"✗ Could not construct primary LLM provider '{primary_name}'")
            return False

        primary_ok = self.primary.authenticate()

        # Fallback is strictly opt-in — see the module docstring.
        fallback_name = get_config('LLM', 'fallback_provider', default='')
        if fallback_name and fallback_name.strip().lower() not in ('none', primary_name.lower()):
            self.fallback = _build_provider(fallback_name, self.profile)
            if self.fallback is not None:
                if self.fallback.authenticate():
                    logger.info(f"✓ LLM fallback ready: {self.fallback.provider_name}")
                else:
                    logger.warning(
                        f"⚠ LLM fallback '{fallback_name}' configured but not currently available"
                    )

        self.enabled = True

        if primary_ok:
            logger.info(f"✓ LLM ready (primary: {self.primary.provider_name})")
        elif self.fallback is not None:
            logger.warning(
                f"⚠ Primary LLM '{primary_name}' unavailable at startup — "
                f"falling back to '{self.fallback.provider_name}', will keep retrying primary"
            )
        else:
            logger.warning(
                f"⚠ Primary LLM '{primary_name}' unavailable at startup — "
                f"will keep retrying, posts use fallback templates until then"
            )

        return True

    # ─────────────────────────────────────────────────────────────────────
    # Availability
    # ─────────────────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """
        True if any configured provider can generate right now.

        Calling this may trigger a reconnect attempt on a downed provider,
        which is exactly what we want: the daemon's poll loop asking "can I use
        AI?" is what heals the connection.
        """
        if not self.enabled:
            return False

        if self.primary is not None and self.primary.is_available():
            if self.using_fallback:
                logger.info(f"✓ Primary LLM ({self.primary.provider_name}) recovered")
                self.using_fallback = False
            return True

        if self.fallback is not None and self.fallback.is_available():
            if not self.using_fallback:
                logger.warning(
                    f"⚠ Primary LLM unavailable, using fallback "
                    f"({self.fallback.provider_name})"
                )
                self.using_fallback = True
            return True

        return False

    @property
    def provider(self) -> Optional[str]:
        """Name of the primary provider, or None if none is configured."""
        return self.primary.provider_name if self.primary else None

    @property
    def active(self) -> Optional[BaseLLM]:
        """The provider that would serve the next request, or None."""
        if self.primary is not None and self.primary.enabled:
            return self.primary
        if self.fallback is not None and self.fallback.enabled:
            return self.fallback
        return None

    def generate(self, prompt: str, max_tokens: Optional[int] = None) -> Optional[str]:
        """
        Generate text from the best available provider.

        Tries the primary, then the fallback. Returns None only if every
        provider failed, in which case the caller should use a static template.
        """
        if not self.enabled:
            return None

        for provider, is_fallback in self._providers_in_order():
            if not provider.is_available():
                continue

            result = provider.generate(prompt, max_tokens=max_tokens)
            if result:
                self.using_fallback = is_fallback
                return result

            logger.warning(f"⚠ {provider.provider_name} returned nothing, trying next provider")

        return None

    def _providers_in_order(self) -> List[Tuple[BaseLLM, bool]]:
        """Providers to try, primary first, paired with an is_fallback flag."""
        order = []
        if self.primary is not None:
            order.append((self.primary, False))
        if self.fallback is not None:
            order.append((self.fallback, True))
        return order

    # ─────────────────────────────────────────────────────────────────────
    # Guardrails — delegated to whichever provider is active
    # ─────────────────────────────────────────────────────────────────────

    def apply_guardrails(self, *args, **kwargs) -> Tuple[Optional[str], List[str]]:
        """Run guardrails using the active provider's configuration."""
        provider = self._guardrail_provider()
        if provider is None:
            return None, ['No LLM provider configured']

        message, issues = provider.apply_guardrails(*args, **kwargs)

        # The provider checked its own (unused) cache; apply the manager's,
        # which is the one that actually spans providers.
        if message and self.is_duplicate_message(message):
            return None, ['Duplicate of a recently posted message']
        if message:
            self.add_to_message_cache(message)

        return message, issues

    def _guardrail_provider(self) -> Optional[BaseLLM]:
        """
        The provider whose guardrail settings and dedup cache we use.

        Deliberately falls back to the primary even when it is down: guardrail
        configuration and the recently-posted cache stay meaningful whether or
        not the server is currently reachable, and failing over to Gemini
        should not reset your duplicate history.
        """
        return self.active or self.primary or self.fallback

    # ─────────────────────────────────────────────────────────────────────
    # Deduplication
    #
    # Held at the manager rather than on a provider so that failing over from
    # Ollama to Gemini doesn't wipe the history of what you just posted.
    # ─────────────────────────────────────────────────────────────────────

    def is_duplicate_message(self, message: str) -> bool:
        """
        True if this is too close to something posted recently.

        Owned here rather than on a provider so it works before authenticate()
        and doesn't reset when we fail over from Ollama to Gemini.
        """
        if not self.enable_deduplication or not self._message_cache:
            return False

        normalized = _normalize_for_dedup(message)

        for cached in self._message_cache:
            cached_norm = _normalize_for_dedup(cached)
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
        """Remember a message so we don't repeat ourselves."""
        if not self.enable_deduplication:
            return

        self._message_cache.append(message)
        if len(self._message_cache) > self.dedup_cache_size:
            self._message_cache = self._message_cache[-self.dedup_cache_size:]

    def status(self) -> Dict[str, Any]:
        """Machine-readable state of every provider, for the health endpoint."""
        return {
            'enabled': self.enabled,
            'available': self.is_available() if self.enabled else False,
            'using_fallback': self.using_fallback,
            'primary': self.primary.status() if self.primary else None,
            'fallback': self.fallback.status() if self.fallback else None,
        }
