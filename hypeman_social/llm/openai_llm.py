# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
OpenAI provider, on the Responses API (OpenAI's recommended endpoint).

Two paths share one request builder:

  generate()             free text, for announcements and chat
  generate_structured()  a strict JSON-schema verdict, for callers such as a
                         safety classifier. The result is strictly parsed
                         (no duplicate keys, NaN or Infinity) and validated
                         against the schema locally, or it is None. A refusal,
                         an incomplete response, bad JSON, or any mismatch is
                         never repaired or guessed at.

Sampling parameters are not sent: the current models do not document them, and
a rejected parameter would fail every call. Reasoning effort is the knob.
"""

import logging
import os
import threading
from typing import Any, Dict, Optional

from hypeman_social.config import get_secret
from hypeman_social.config.config import get_exact_config
from hypeman_social.llm._cloud import (
    RateLimiter,
    check_schema_supported,
    classify_sdk_error,
    cloud_setting,
    parse_number,
    parse_strict_json,
    validate_against_schema,
    validate_base_url,
)
from hypeman_social.llm.base import BaseLLM
from hypeman_social.llm.profiles import GENERIC_PROFILE, ContentProfile

try:
    import openai
    OPENAI_AVAILABLE = True
except ImportError:
    # Placeholder for the optional SDK; every use is gated on *_AVAILABLE.
    openai = None  # type: ignore[assignment]  # optional dependency not installed
    OPENAI_AVAILABLE = False

logger = logging.getLogger(__name__)

DEFAULT_MODEL = 'gpt-6-luna'


class OpenAILLM(BaseLLM):
    """OpenAI Responses API generation with an optional strict structured path."""

    provider_name = 'openai'

    def __init__(self, profile: ContentProfile = GENERIC_PROFILE):
        super().__init__(profile=profile)
        self.client: Optional[Any] = None
        self.model: Optional[str] = None
        self.api_key: Optional[str] = None

        # Every setting is read from its LLM_OPENAI_* name only (see
        # cloud_setting): an ambient OPENAI_BASE_URL or OPENAI_MODEL must not
        # beat it, least of all the endpoint, which receives the API key.
        self.system_prompt = cloud_setting('OPENAI', 'system_prompt', '') or ''
        self.timeout = parse_number(
            'LLM_OPENAI_TIMEOUT', cloud_setting('OPENAI', 'timeout'), 30.0)
        self.base_url = cloud_setting('OPENAI', 'base_url') or None
        # An endpoint that fails validation is never used: authenticate() fails
        # closed rather than quietly sending the key to the official host.
        self.base_url_rejected = False
        if self.base_url and not validate_base_url(self.base_url):
            logger.error(
                "✗ LLM_OPENAI_BASE_URL rejected (needs https, or http on loopback; "
                "no credentials, query or fragment in the URL)")
            self.base_url, self.base_url_rejected = None, True
        # 'omit' leaves the model default; 'none' is a real effort level and is sent.
        self.reasoning_effort = (
            cloud_setting('OPENAI', 'reasoning_effort', 'low') or 'low').strip().lower()
        self.limiter = RateLimiter(parse_number(
            'LLM_OPENAI_RATE_LIMIT', cloud_setting('OPENAI', 'rate_limit'), 60,
            integer=True, allow_zero=True))

        # The schema for the call in flight on this thread; see generate_structured.
        self._local = threading.local()

    @property
    def rate_limit(self) -> int:
        return self.limiter.per_minute

    def provider_config(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """LLM_OPENAI_<KEY>, then the shared LLM_<KEY>; never a bare ambient name."""
        return cloud_setting('OPENAI', key) or get_exact_config(f'LLM_{key}'.upper(), default)

    def set_system_prompt(self, text: str) -> None:
        self.system_prompt = text or ''

    def authenticate(self) -> bool:
        """Read the API key and build a client (verified on first use, not here)."""
        if not OPENAI_AVAILABLE:
            logger.error("✗ OpenAI client not installed. Run: pip install 'hypeman-social[openai]'")
            return False

        if self.base_url_rejected:
            return False

        try:
            scope = 'LLM'
            self.api_key = get_secret(
                scope, 'openai_api_key',
                secret_name_env=f'SECRETS_AWS_{scope}_SECRET_NAME',
                secret_path_env=f'SECRETS_VAULT_{scope}_SECRET_PATH',
                doppler_secret_env=f'SECRETS_DOPPLER_{scope}_SECRET_NAME',
            )
            if not self.api_key:
                self.api_key = os.getenv('OPENAI_API_KEY')

            if not self.api_key:
                logger.error("✗ OpenAI enabled but no API key found")
                return False

            self.model = self.provider_config('model', default=DEFAULT_MODEL)
            self._configured = True
            self.client = self._build_client()

            self.enabled = True
            self._connection_was_successful = True
            logger.info(f"✓ OpenAI initialized (model: {self.model}, {self.rate_limit} req/min)")
            return True

        except Exception as e:
            logger.error(f"✗ Failed to initialize OpenAI: {type(e).__name__}")
            self._last_error = type(e).__name__
            self.enabled = False
            return False

    def _build_client(self) -> Any:
        # max_retries=0: the base class owns retry and backoff.
        kwargs: Dict[str, Any] = {
            'api_key': self.api_key, 'timeout': self.timeout, 'max_retries': 0,
        }
        if self.base_url:
            kwargs['base_url'] = self.base_url
        return openai.OpenAI(**kwargs)

    def _reconnect(self) -> bool:
        if not OPENAI_AVAILABLE or not self.api_key:
            return False
        try:
            self.client = self._build_client()
            return True
        except Exception as e:
            self._last_error = type(e).__name__
            return False

    def _build_request(self, prompt: str, max_tokens: int) -> Dict[str, Any]:
        request: Dict[str, Any] = {
            'model': self.model,
            'input': prompt,
            'max_output_tokens': max_tokens,
            # Not stored server-side: these are chat/classifier calls.
            'store': False,
        }
        if self.system_prompt:
            request['instructions'] = self.system_prompt
        if self.reasoning_effort and self.reasoning_effort != 'omit':
            request['reasoning'] = {'effort': self.reasoning_effort}
        schema = getattr(self._local, 'schema', None)
        if schema is not None:
            request['text'] = {'format': {
                'type': 'json_schema',
                'name': schema['name'],
                'strict': True,
                'schema': schema['schema'],
            }}
        return request

    def _raw_generate(self, prompt: str, max_tokens: int) -> Optional[str]:
        """One Responses API call. Raises a key-free error the base can classify."""
        if not self.client:
            raise ConnectionError("OpenAI client not initialized")

        self.limiter.wait()

        # Classify inside the handler, raise outside it, so the credential-bearing
        # SDK error is not attached as __context__ (`from None` hides only __cause__).
        failure: Optional[str] = None
        try:
            response = self.client.responses.create(**self._build_request(prompt, max_tokens))
        except Exception as e:
            failure = classify_sdk_error(e)
        if failure is not None:
            raise RuntimeError(failure)

        structured = getattr(self._local, 'schema', None) is not None

        # Truncated output is never usable as a verdict, and rarely as a post.
        if getattr(response, 'status', None) == 'incomplete' and structured:
            logger.warning("⚠ OpenAI response incomplete; discarding structured result")
            return None

        for item in getattr(response, 'output', None) or []:
            if getattr(item, 'type', None) != 'message':
                continue
            for part in getattr(item, 'content', None) or []:
                if getattr(part, 'type', None) == 'refusal':
                    logger.warning("⚠ OpenAI declined the request (refusal)")
                    return None

        text = getattr(response, 'output_text', None)
        return text.strip() if text and text.strip() else None

    def generate_structured(
        self,
        prompt: str,
        schema: Dict[str, Any],
        name: str = 'result',
        max_tokens: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Generate a JSON object that conforms to ``schema``, or None.

        Goes through generate(), so availability, throttling, retry and error
        classification are the same as the free-text path. None means refused,
        incomplete, unparseable, or not matching the schema (types, required,
        additionalProperties, enum, items); a schema using any other keyword is
        refused before the call;
        callers must treat it as "no verdict" and fail closed.
        """
        try:
            check_schema_supported(schema)
        except ValueError:
            logger.warning("⚠ Structured schema uses unsupported keywords; refusing to call")
            return None

        self._local.schema = {'name': name, 'schema': schema}
        try:
            raw = self.generate(prompt, max_tokens=max_tokens)
        finally:
            self._local.schema = None

        if not raw:
            return None
        try:
            data = parse_strict_json(raw)
        except (ValueError, RecursionError):
            logger.warning("⚠ OpenAI structured output was not strict JSON; treating as refusal")
            return None
        if not isinstance(data, dict) or not validate_against_schema(data, schema):
            logger.warning("⚠ OpenAI structured output did not match the schema; treating as refusal")
            return None
        return data
