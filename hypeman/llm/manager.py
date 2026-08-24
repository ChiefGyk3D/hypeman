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
        provider = self.active or self.primary
        if provider is None:
            return None, ['No LLM provider configured']
        return provider.apply_guardrails(*args, **kwargs)

    def status(self) -> Dict[str, Any]:
        """Machine-readable state of every provider, for the health endpoint."""
        return {
            'enabled': self.enabled,
            'available': self.is_available() if self.enabled else False,
            'using_fallback': self.using_fallback,
            'primary': self.primary.status() if self.primary else None,
            'fallback': self.fallback.status() if self.fallback else None,
        }
