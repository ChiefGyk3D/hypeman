# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Ollama provider — local, private, free-except-for-electricity LLM generation.

George Carlin would've loved this: we built computers to do our thinking for us,
and now we're using them to write notifications about cat videos. What a time to
be alive. The AI doesn't judge your content — it just generates the post and
moves on with its life. Unlike your relatives, who still don't understand what
you do for a living.

The one thing this file does differently from its ancestors: a failed
authenticate() is NOT fatal. We keep the host and model configuration, mark
ourselves unavailable, and let is_available() bring us back when the server
returns. Taking your AI box offline should cost you some template-fallback
posts, not a daemon restart.
"""

import logging
from typing import Any, Optional

from hypeman_social.config import get_config
from hypeman_social.llm import guardrails
from hypeman_social.llm.base import BaseLLM
from hypeman_social.llm.profiles import GENERIC_PROFILE, ContentProfile

try:
    import ollama
    OLLAMA_AVAILABLE = True
except ImportError:
    OLLAMA_AVAILABLE = False

logger = logging.getLogger(__name__)


class OllamaLLM(BaseLLM):
    """
    Local LLM generation via an Ollama server.

    No cloud API costs, no rate limits, and your video titles never leave your
    network. Supports gemma3, qwen2.5/3, llama3, mistral, and anything else
    Ollama will load.
    """

    provider_name = 'ollama'

    def __init__(self, profile: ContentProfile = GENERIC_PROFILE):
        super().__init__(profile=profile)
        self.client: Optional[Any] = None
        self.host: Optional[str] = None
        self.model: Optional[str] = None

        # True when the loaded model advertises the 'thinking' capability.
        # Detected at connect time; controls whether we pass `think` at all,
        # so older Ollama servers that predate the parameter never see it.
        self._supports_thinking = False

    def authenticate(self) -> bool:
        """
        Read config and connect to the Ollama server.

        Returns False if the server is unreachable — but crucially, config is
        retained and _configured is set, so is_available() can reconnect later.
        A False here means "not right now", not "never".
        """
        if not OLLAMA_AVAILABLE:
            logger.error("✗ Ollama Python client not installed. Run: pip install ollama")
            return False

        try:
            host = self.provider_config('host') or get_config(
                'LLM', 'ollama_host', default='http://localhost')
            port = self.provider_config('port') or get_config(
                'LLM', 'ollama_port', default='11434')
            # LLM_OLLAMA_MODEL wins over the shared LLM_MODEL, so a Gemini
            # fallback and a local Ollama primary can each name their own.
            self.model = self.provider_config('model', default='gemma3:4b')

            if not host.startswith('http'):
                host = f"http://{host}"
            # Only append the port if the host doesn't already carry one.
            self.host = host if ':' in host.split('//')[-1] else f"{host}:{port}"

            # We now know where the server should be. Even if the connection
            # below fails, recovery has a target to aim at.
            self._configured = True

            if self.enable_thinking_mode:
                logger.info(
                    f"🧠 Thinking mode enabled "
                    f"(token multiplier: {self.thinking_token_multiplier}x)"
                )

            if not self._reconnect():
                logger.error(
                    f"✗ Failed to connect to Ollama at {self.host} — "
                    f"will retry automatically every {self.reconnect_interval}s"
                )
                return False

            self.enabled = True
            self._connection_was_successful = True
            self._reconnect_attempt_count = 0
            logger.info(f"✓ Ollama initialized (host: {self.host}, model: {self.model})")
            return True

        except Exception as e:
            # Log the actual exception. The original swallowed it, which made
            # "Failed to connect" impossible to debug.
            logger.error(f"✗ Failed to initialize Ollama: {type(e).__name__}: {e}")
            self._last_error = f"{type(e).__name__}: {e}"
            self.enabled = False
            return False

    def _reconnect(self) -> bool:
        """
        Open a client and verify the server answers.

        Raises nothing — returns False on any failure so the cooldown logic in
        BaseLLM._attempt_recovery stays in charge of pacing.
        """
        if not OLLAMA_AVAILABLE or not self.host:
            return False

        try:
            client = ollama.Client(host=self.host)
            response = client.list()

            # Warn if the configured model isn't present, but don't fail —
            # Ollama will pull it on first use.
            models = self._model_names(response)
            if models and self.model not in models:
                logger.warning(
                    f"⚠ Model '{self.model}' not on the Ollama server. "
                    f"Available: {', '.join(models)}. Run: ollama pull {self.model}"
                )

            self.client = client
            self._supports_thinking = self._detect_thinking_support(client)
            logger.debug(
                f"Connected to Ollama at {self.host} "
                f"(thinking-capable model: {self._supports_thinking})"
            )
            return True

        except Exception as e:
            self._last_error = f"{type(e).__name__}: {e}"
            logger.debug(f"Ollama connection attempt failed: {type(e).__name__}: {e}")
            return False

    def _detect_thinking_support(self, client) -> bool:
        """
        True if the configured model advertises the 'thinking' capability.

        Hybrid reasoning models (gemma4, qwen3, deepseek-r1) think by default
        when the request doesn't say otherwise. On generate() that reasoning is
        withheld from the response, so the model can burn the entire num_predict
        budget "thinking" and hand back an empty string — which callers see as
        a dead provider. Knowing the capability lets _raw_generate pass an
        explicit `think` and stay in control of the output.

        Detection failure (old server without /api/show capabilities, model not
        yet pulled) just means False — we then never send `think`, which is the
        pre-existing behaviour.
        """
        if not self.model:
            return False

        try:
            info = client.show(self.model)
        except Exception:
            return False

        capabilities = getattr(info, 'capabilities', None)
        if capabilities is None and isinstance(info, dict):
            capabilities = info.get('capabilities')

        return bool(capabilities) and 'thinking' in (capabilities or ())

    @staticmethod
    def _model_names(response) -> list:
        """Pull model names out of a list() response across ollama client versions."""
        raw = None
        if hasattr(response, 'models'):
            raw = response.models
        elif isinstance(response, dict) and 'models' in response:
            raw = response['models']

        if not raw:
            return []

        names = []
        for entry in raw:
            if isinstance(entry, dict):
                name = entry.get('name') or entry.get('model')
            else:
                name = getattr(entry, 'name', None) or getattr(entry, 'model', None)
            if name:
                names.append(name)
        return names

    def _raw_generate(self, prompt: str, max_tokens: int) -> Optional[str]:
        """
        One generation call against Ollama.

        Raises on transport failure so BaseLLM.generate can classify the error
        and decide whether to retry, reconnect, or give up.
        """
        if not self.client:
            raise ConnectionError("Ollama client not connected")

        # For thinking-capable models the request must say whether to reason:
        # left unspecified they think by default, and generate() withholds the
        # reasoning — every token goes to thoughts we never see and the content
        # comes back empty. think=False forces a direct answer; think=True
        # routes the reasoning into the response's thinking field, where the
        # extraction below can reach it.
        extra = {}
        if self._supports_thinking:
            extra['think'] = bool(self.enable_thinking_mode)

        response = self.client.generate(
            model=self.model,
            prompt=prompt,
            options={
                'num_predict': max_tokens,
                'temperature': self.temperature,
                'top_p': self.top_p,
            },
            **extra,
        )

        result, thinking = self._unpack(response)

        # Reasoning models sometimes burn the whole budget thinking and return
        # an empty content field. The post is usually in the thinking text.
        if (not result or len(result) < 20) and thinking and self.enable_thinking_mode:
            logger.info("Content empty, extracting from thinking field...")
            extracted = guardrails.extract_from_thinking(thinking, max_tokens * 3)
            if extracted:
                logger.info(f"Extracted {len(extracted)} chars from thinking mode")
                return extracted

        return result

    @staticmethod
    def _unpack(response):
        """
        Normalise an Ollama response into (content, thinking).

        generate() puts thinking at the top level; chat() nests it under
        message. Both dict and Pydantic shapes show up depending on version.
        """
        if isinstance(response, dict):
            content = (response.get('response') or '').strip()
            thinking = response.get('thinking') or ''
            message = response.get('message')
            if isinstance(message, dict):
                thinking = thinking or message.get('thinking', '')
                content = content or (message.get('content') or '').strip()
            return content, thinking

        if hasattr(response, 'response'):
            content = (response.response or '').strip()
            thinking = getattr(response, 'thinking', '') or ''
            if not thinking:
                message = getattr(response, 'message', None)
                thinking = getattr(message, 'thinking', '') or '' if message else ''
            return content, thinking

        return str(response).strip(), ''
