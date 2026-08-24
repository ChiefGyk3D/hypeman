# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Google Gemini provider — the cloud option.

Costs nothing on the free tier, needs no GPU in your closet, and works when
your local AI server is underwater. In exchange, Google learns what you stream.
Pick your poison; hypeman supports running Ollama as primary with Gemini as the
failover, which is the sensible compromise.
"""

import logging
import os
import threading
import time
from typing import Optional

from hypeman.config import get_config, get_secret
from hypeman.llm.base import BaseLLM
from hypeman.llm.profiles import ContentProfile, GENERIC_PROFILE

try:
    from google import genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

logger = logging.getLogger(__name__)


class GeminiLLM(BaseLLM):
    """
    Gemini API generation with client-side rate limiting.

    The free tier is generous but finite, so we self-throttle rather than
    discovering the limit via 429s at the worst possible moment.
    """

    provider_name = 'gemini'

    # 429 and 503 are worth retrying; the base class handles the rest.
    PERMANENT_ERROR_MARKERS = ('invalid', 'unauthorized', 'forbidden', 'api key not valid')

    def __init__(self, profile: ContentProfile = GENERIC_PROFILE):
        super().__init__(profile=profile)
        self.client = None
        self.model: Optional[str] = None
        self.api_key: Optional[str] = None

        # Client-side rate limiting, shared across threads.
        self.rate_limit = int(get_config('LLM', 'gemini_rate_limit', default='30'))
        self._rate_lock = threading.Lock()
        self._last_call = 0.0

    def authenticate(self) -> bool:
        """
        Read the API key and construct a Gemini client.

        Returns:
            True if a client was created. Note that Gemini doesn't verify the
            key until first use, so this succeeding isn't proof the key works.
        """
        if not GEMINI_AVAILABLE:
            logger.error("✗ Gemini client not installed. Run: pip install google-genai")
            return False

        try:
            self.api_key = get_secret(
                'LLM', 'gemini_api_key',
                secret_name_env='SECRETS_AWS_LLM_SECRET_NAME',
                secret_path_env='SECRETS_VAULT_LLM_SECRET_PATH',
                doppler_secret_env='SECRETS_DOPPLER_LLM_SECRET_NAME',
            )

            # GEMINI_API_KEY unprefixed is the common Doppler/CI convention.
            if not self.api_key:
                self.api_key = os.getenv('GEMINI_API_KEY')

            if not self.api_key:
                logger.error("✗ Gemini enabled but no API key found")
                return False

            self.model = self.provider_config('model', default='gemini-2.0-flash-lite')
            self._configured = True

            self.client = genai.Client(api_key=self.api_key)

            self.enabled = True
            self._connection_was_successful = True
            logger.info(
                f"✓ Gemini initialized (model: {self.model}, {self.rate_limit} req/min)"
            )
            return True

        except Exception as e:
            logger.error(f"✗ Failed to initialize Gemini: {type(e).__name__}: {e}")
            self._last_error = f"{type(e).__name__}: {e}"
            self.enabled = False
            return False

    def _reconnect(self) -> bool:
        """Rebuild the client. Cheap, since Gemini is stateless HTTP."""
        if not GEMINI_AVAILABLE or not self.api_key:
            return False

        try:
            self.client = genai.Client(api_key=self.api_key)
            return True
        except Exception as e:
            self._last_error = f"{type(e).__name__}: {e}"
            return False

    def _throttle(self) -> None:
        """Space out calls to stay under the configured requests-per-minute."""
        if self.rate_limit <= 0:
            return

        min_interval = 60.0 / self.rate_limit
        with self._rate_lock:
            elapsed = time.time() - self._last_call
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
            self._last_call = time.time()

    def _raw_generate(self, prompt: str, max_tokens: int) -> Optional[str]:
        """One generation call against the Gemini API."""
        if not self.client:
            raise ConnectionError("Gemini client not initialized")

        self._throttle()

        response = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config={
                'max_output_tokens': max_tokens,
                'temperature': self.temperature,
                'top_p': self.top_p,
            },
        )

        text = getattr(response, 'text', None)
        return text.strip() if text else None
