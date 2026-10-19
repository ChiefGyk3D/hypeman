# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Anthropic Claude provider.

Built for high-volume chat, so the defaults are the cheap path: the Haiku
model, low effort, thinking off where the model allows it, and a cached system
block so the persona/rules prefix is billed at the cache-read rate after the
first call.

Parameters follow the Claude API docs (platform.claude.com): Haiku 5.5 and
Sonnet 5.5 reject non-default sampling parameters, so temperature/top_p are
never sent; steer behaviour from the prompt instead.
"""

import logging
import os
from typing import Any, Dict, List, Optional

from hypeman_social.config import get_bool_config, get_config, get_secret
from hypeman_social.llm._cloud import RateLimiter, classify_sdk_error
from hypeman_social.llm.base import BaseLLM
from hypeman_social.llm.profiles import GENERIC_PROFILE, ContentProfile

try:
    import anthropic
    CLAUDE_AVAILABLE = True
except ImportError:
    anthropic = None
    CLAUDE_AVAILABLE = False

logger = logging.getLogger(__name__)

DEFAULT_MODEL = 'claude-haiku-5-5'

# 'auto' picks per model; the rest are sent as given.
THINKING_MODES = ('auto', 'disabled', 'adaptive', 'between_tools', 'omit')


class ClaudeLLM(BaseLLM):
    """Claude Messages API generation with prompt caching and a client-side limiter."""

    provider_name = 'claude'

    def __init__(self, profile: ContentProfile = GENERIC_PROFILE):
        super().__init__(profile=profile)
        self.client: Optional[Any] = None
        self.model: Optional[str] = None
        self.api_key: Optional[str] = None

        # Persona/rules prefix. Identical across calls so it can be cached.
        self.system_prompt = get_config('LLM', 'claude_system_prompt', default='') or ''
        self.enable_prompt_cache = get_bool_config('LLM', 'claude_prompt_cache', default=True)
        # '5m' (default, 1.25x write) or '1h' (2x write, for sparse traffic).
        self.cache_ttl = get_config('LLM', 'claude_cache_ttl', default='5m')
        self.timeout = float(get_config('LLM', 'claude_timeout', default='30'))
        # Not 'claude_effort': get_config tries the bare key CLAUDE_EFFORT first,
        # and that name is already used by other Claude tooling. 'omit' (an empty
        # value counts as unset) leaves the model's own default in place.
        self.effort = get_config('LLM', 'claude_effort_level', default='low').strip().lower()
        self.thinking = (get_config('LLM', 'claude_thinking', default='auto') or 'auto').lower()
        self.limiter = RateLimiter(int(get_config('LLM', 'claude_rate_limit', default='50')))

    @property
    def rate_limit(self) -> int:
        return self.limiter.per_minute

    def set_system_prompt(self, text: str) -> None:
        """Set the stable persona/rules prefix that gets the cache breakpoint."""
        self.system_prompt = text or ''

    def authenticate(self) -> bool:
        """
        Read the API key and build a client. The key is not verified until
        first use, so True means "configured", not "proven".
        """
        if not CLAUDE_AVAILABLE:
            logger.error("✗ Anthropic client not installed. Run: pip install 'hypeman-social[claude]'")
            return False

        try:
            scope = 'LLM'
            self.api_key = get_secret(
                scope, 'claude_api_key',
                secret_name_env=f'SECRETS_AWS_{scope}_SECRET_NAME',
                secret_path_env=f'SECRETS_VAULT_{scope}_SECRET_PATH',
                doppler_secret_env=f'SECRETS_DOPPLER_{scope}_SECRET_NAME',
            )
            if not self.api_key:
                self.api_key = os.getenv('ANTHROPIC_API_KEY')

            if not self.api_key:
                logger.error("✗ Claude enabled but no API key found")
                return False

            self.model = self.provider_config('model', default=DEFAULT_MODEL)
            self._configured = True
            self.client = self._build_client()

            self.enabled = True
            self._connection_was_successful = True
            logger.info(
                f"✓ Claude initialized (model: {self.model}, {self.rate_limit} req/min, "
                f"cache: {'on' if self.enable_prompt_cache else 'off'})"
            )
            return True

        except Exception as e:
            logger.error(f"✗ Failed to initialize Claude: {type(e).__name__}")
            self._last_error = type(e).__name__
            self.enabled = False
            return False

    def _build_client(self) -> Any:
        # max_retries=0: the base class owns retry and backoff. Leaving the
        # SDK's own retries on would multiply attempts and hide failures from it.
        return anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout, max_retries=0)

    def _reconnect(self) -> bool:
        """Rebuild the client; the API is stateless HTTP."""
        if not CLAUDE_AVAILABLE or not self.api_key:
            return False
        try:
            self.client = self._build_client()
            return True
        except Exception as e:
            self._last_error = type(e).__name__
            return False

    def _system_blocks(self) -> Optional[List[Dict[str, Any]]]:
        """The system prompt as one block, with the cache breakpoint when enabled."""
        if not self.system_prompt:
            return None
        block: Dict[str, Any] = {'type': 'text', 'text': self.system_prompt}
        if self.enable_prompt_cache:
            cache: Dict[str, Any] = {'type': 'ephemeral'}
            if self.cache_ttl == '1h':
                cache['ttl'] = '1h'
            block['cache_control'] = cache
        return [block]

    def _thinking_param(self) -> Optional[Dict[str, str]]:
        mode = self.thinking if self.thinking in THINKING_MODES else 'auto'
        if mode == 'omit':
            return None
        if mode == 'auto':
            # Haiku 5.5 allows thinking off at low effort, and chat does not
            # need it. Other models reject 'disabled' or run it by default.
            if (self.model or '').startswith('claude-haiku-5'):
                return {'type': 'disabled'}
            return None
        return {'type': mode}

    def _build_request(self, prompt: str, max_tokens: int) -> Dict[str, Any]:
        request: Dict[str, Any] = {
            'model': self.model,
            'max_tokens': max_tokens,
            'messages': [{'role': 'user', 'content': prompt}],
        }
        system = self._system_blocks()
        if system:
            request['system'] = system
        thinking = self._thinking_param()
        if thinking:
            request['thinking'] = thinking
        if self.effort and self.effort != 'omit':
            request['output_config'] = {'effort': self.effort}
        return request

    def _raw_generate(self, prompt: str, max_tokens: int) -> Optional[str]:
        """One Messages API call. Raises a key-free error the base can classify."""
        if not self.client:
            raise ConnectionError("Claude client not initialized")

        self.limiter.wait()

        try:
            response = self.client.messages.create(**self._build_request(prompt, max_tokens))
        except Exception as e:
            raise RuntimeError(classify_sdk_error(e)) from None

        # A safety decline is a normal 200; it is "no answer", not an error.
        if getattr(response, 'stop_reason', None) == 'refusal':
            logger.warning("⚠ Claude declined the request (refusal)")
            return None

        parts = [
            block.text for block in (getattr(response, 'content', None) or [])
            if getattr(block, 'type', None) == 'text' and getattr(block, 'text', None)
        ]
        text = ''.join(parts).strip()
        return text or None
