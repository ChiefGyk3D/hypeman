# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Shared helpers for the cloud API providers (Claude, OpenAI).

Kept in one place so both providers throttle and classify errors identically.
Gemini predates this module and carries its own copy of the throttle; it is
left alone on purpose.
"""

import ipaddress
import json
import logging
import math
import ssl
import threading
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

from hypeman_social.config.config import get_config, get_exact_config

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Client-side requests-per-minute limiter, safe to share across threads.

    A limit of 0 (or less) disables throttling. Spacing calls evenly is
    coarser than a token bucket but cheap, predictable, and enough to stay
    under a provider's published requests-per-minute without discovering the
    limit via 429s.
    """

    def __init__(
        self,
        per_minute: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.per_minute = per_minute
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_call: Optional[float] = None

    def wait(self) -> None:
        """Block until the next call is allowed."""
        if self.per_minute <= 0:
            return
        min_interval = 60.0 / self.per_minute
        with self._lock:
            now = self._clock()
            if self._last_call is not None:
                elapsed = now - self._last_call
                if elapsed < min_interval:
                    self._sleep(min_interval - elapsed)
                    now = self._clock()
            self._last_call = now


class ProviderError(RuntimeError):
    """
    A provider failure whose message is only a safe, fixed-vocabulary reason.

    What is guaranteed: it is raised after the SDK handler has exited, so on the
    provider's own call path it carries no __context__ and no __cause__, its
    message never contains SDK error text, and it is marked
    ``__suppress_context__`` so a formatted traceback prints no chained
    exception even when the CALLER is inside an ``except`` block (or across an
    ``await``) and Python attaches that outer exception at raise time.

    What is not claimed: an exception object a caller's own handler attaches
    stays reachable from ``__context__`` and could hold a credential. Object
    level secrecy is not provided, only safe messages and safe formatting.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.__cause__ = None
        self.__suppress_context__ = True


def classify_sdk_error(error: BaseException) -> str:
    """Exception-safe wrapper: a hostile or broken error object yields a generic message."""
    try:
        return _classify(error)
    except Exception:
        return 'unexpected provider error'


def _classify(error: BaseException) -> str:
    """
    Turn an SDK exception into a short, key-free message the base class can
    classify by substring.

    The original text is deliberately dropped: SDK error bodies can echo
    request details, and the base class logs and stores what we raise. Works
    on class names (walking the MRO) and an optional ``status_code`` so it
    needs no SDK import and behaves the same for both vendors.
    """
    names = {cls.__name__ for cls in type(error).__mro__}
    status = getattr(error, 'status_code', None)

    # Timeout first: in both SDKs it subclasses the connection error.
    if 'APITimeoutError' in names or 'Timeout' in names or 'TimeoutException' in names:
        return 'request timed out'
    if 'RateLimitError' in names or status == 429:
        return 'rate limit (429)'
    if 'AuthenticationError' in names or status == 401:
        return 'unauthorized (401)'
    if 'PermissionDeniedError' in names or status == 403:
        return 'forbidden (403)'
    if 'NotFoundError' in names or status == 404:
        return 'not found (404)'
    if 'BadRequestError' in names or status in (400, 413, 422):
        return f'invalid request ({status or 400})'
    if 'APIConnectionError' in names:
        return 'connection error'
    if isinstance(status, int) and status >= 500:
        return f'server unavailable or overloaded ({status})'
    return f'unexpected provider error ({type(error).__name__})'


# ─────────────────────────────────────────────────────────────────────────────
# Configuration: namespaced keys only, defensively parsed
# ─────────────────────────────────────────────────────────────────────────────

def cloud_setting(provider: str, key: str, default: Optional[str] = None) -> Optional[str]:
    """
    Read ``LLM_<PROVIDER>_<KEY>`` (Doppler first, then that exact env name).

    Deliberately not get_config(): that tries the bare key first, so an
    ambient ``OPENAI_BASE_URL`` or ``MODEL`` in the process environment would
    beat the namespaced setting. For a provider that holds a credential that
    is a redirect risk, so these providers read only their own namespace.
    """
    return get_exact_config(f'LLM_{provider}_{key}'.upper(), default)


def parse_number(
    setting: str,
    raw: Optional[str],
    default: float,
    *,
    integer: bool = False,
    allow_zero: bool = False,
) -> Any:
    """
    Parse a numeric setting without ever raising.

    Accepts only finite values that are positive (or zero when ``allow_zero``).
    Anything else logs one warning that names the setting, never the value,
    and returns ``default``. An unset or empty value is silently the default.
    """
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip()) if integer else float(str(raw).strip())
        ok = math.isfinite(value) and (value >= 0 if allow_zero else value > 0)
    except (ValueError, OverflowError):
        ok = False
    if not ok:
        logger.warning(f"⚠ Invalid value for {setting}; using the default ({default})")
        return default
    return value


_LOOPBACK_NAMES = ('localhost',)


def validate_base_url(url: str) -> bool:
    """
    True if ``url`` is safe to send a credential to.

    Requires https (plain http only for a loopback host), a host, and no
    userinfo, query or fragment; control characters and whitespace are out.
    """
    if not url or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname
        _ = parts.port  # raises ValueError on a malformed port
    except ValueError:
        return False
    if not host or '@' in parts.netloc or parts.username is not None or parts.password is not None:
        return False
    if parts.query or parts.fragment or '?' in url or '#' in url:
        return False
    if parts.scheme == 'https':
        return True
    if parts.scheme == 'http':
        if host in _LOOPBACK_NAMES:
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False
    return False


def validate_proxy_url(url: str) -> bool:
    """True for an http(s) proxy URL with a host, a sane port and no userinfo, query or fragment."""
    if not url or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname
        _ = parts.port  # raises ValueError on a malformed port
    except ValueError:
        return False
    return (
        parts.scheme in ('http', 'https') and bool(host)
        and '@' not in parts.netloc and parts.username is None and parts.password is None
        and not parts.query and not parts.fragment and '?' not in url and '#' not in url
    )


def validate_ca_bundle(path: str) -> bool:
    """True if ``path`` is a readable file that loads as a PEM CA bundle."""
    try:
        ssl.create_default_context(cafile=path)  # raises if missing, unreadable, a directory or not PEM
        return True
    except (OSError, ValueError, ssl.SSLError):
        return False


def build_http_client(proxy: Optional[str] = None, ca_bundle: Optional[str] = None) -> Any:
    """
    The HTTP client both cloud SDKs use, with trust taken only from settings.

    trust_env=False: HTTP(S)_PROXY, ALL_PROXY, NO_PROXY and SSL_CERT_FILE/DIR in
    the process environment are ignored, because anyone who controls them can
    terminate TLS for the pinned host with their own CA and read the key. A
    private proxy or CA is configured through LLM_<PROVIDER>_PROXY / _CA_BUNDLE
    instead. A CA bundle REPLACES the default trust store. Redirects are off.
    The SDKs' own DefaultHttpxClient is not used: Anthropic's reads proxy
    variables itself even with trust_env=False.
    """
    import httpx2  # installed with both SDKs; imported late so the extras contract holds

    kwargs: Dict[str, Any] = {'follow_redirects': False, 'trust_env': False}
    if proxy:
        kwargs['proxy'] = proxy
    if ca_bundle:
        kwargs['verify'] = ssl.create_default_context(cafile=ca_bundle)
    return httpx2.Client(**kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# Structured output: strict parse + local schema validation
# ─────────────────────────────────────────────────────────────────────────────

_SUPPORTED_KEYWORDS = frozenset({
    'type', 'properties', 'required', 'additionalProperties', 'enum', 'items',
    'description', 'title',  # annotations only, no validation effect
})
_TYPES = frozenset({'object', 'array', 'string', 'number', 'integer', 'boolean', 'null'})
_MAX_SCHEMA_DEPTH = 32
_MAX_ENUM_OPTIONS = 256
_MAX_ENUM_NODES = 2000
_MAX_ENUM_DEPTH = 8
_MAX_ENUM_TEXT = 16384  # characters of strings and object keys, in aggregate

OPENAI_DEFAULT_BASE_URL = 'https://api.openai.com/v1'
ANTHROPIC_DEFAULT_BASE_URL = 'https://api.anthropic.com'


def _reject_constant(name: str) -> Any:
    raise ValueError(f'non-finite JSON constant {name}')


def _reject_nonfinite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError('non-finite JSON number')
    return value


def _no_duplicate_keys(pairs: list) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate JSON key')
        out[key] = value
    return out


def parse_strict_json(raw: str) -> Any:
    """json.loads that refuses duplicate keys, NaN/Infinity and overflowing floats."""
    return json.loads(
        raw,
        object_pairs_hook=_no_duplicate_keys,
        parse_constant=_reject_constant,
        parse_float=_reject_nonfinite_float,
    )


def _check_enum(options: Any) -> None:
    """
    Bound an enum's size, nesting and text, allowing only plain JSON values.

    Iterative with bounded auxiliary memory: a container is rejected from its
    len() against the remaining node budget BEFORE any of it is iterated or
    pushed, so a hostile 100,000-item list costs nothing.
    """
    if not isinstance(options, list) or len(options) > _MAX_ENUM_OPTIONS:
        raise ValueError('enum must be a short list')
    nodes = len(options)  # every node is counted when it is queued
    text = 0
    stack = [(option, 1) for option in options]
    while stack:
        value, depth = stack.pop()
        if depth > _MAX_ENUM_DEPTH:
            raise ValueError('enum values too deep')
        if isinstance(value, (dict, list)):
            if nodes + len(value) > _MAX_ENUM_NODES:
                raise ValueError('enum values too large')
            nodes += len(value)
            if isinstance(value, dict):
                for key, child in value.items():
                    if not isinstance(key, str):
                        raise ValueError('enum object keys must be strings')
                    text += len(key)
                    if text > _MAX_ENUM_TEXT:
                        raise ValueError('enum text too large')
                    stack.append((child, depth + 1))
            else:
                stack.extend((child, depth + 1) for child in value)
        elif isinstance(value, str):
            text += len(value)
            if text > _MAX_ENUM_TEXT:
                raise ValueError('enum text too large')
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError('enum numbers must be finite')
        elif value is not None and not isinstance(value, (bool, int)):
            raise ValueError('enum values must be JSON values')


def check_schema_supported(schema: Any, _depth: int = 0) -> None:
    """Raise ValueError if the schema uses anything the local validator cannot enforce."""
    if _depth > _MAX_SCHEMA_DEPTH or not isinstance(schema, dict):
        raise ValueError('schema is not a (shallow enough) object')
    unknown = set(schema) - _SUPPORTED_KEYWORDS
    if unknown:
        raise ValueError(f'unsupported schema keywords: {sorted(unknown)}')
    declared = schema.get('type')
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not names or any(not isinstance(n, str) or n not in _TYPES for n in names):
            raise ValueError('unsupported schema type')
    if 'required' in schema and not (
            isinstance(schema['required'], list) and all(isinstance(k, str) for k in schema['required'])):
        raise ValueError('required must be a list of strings')
    if 'enum' in schema:
        _check_enum(schema['enum'])
    if 'additionalProperties' in schema and not isinstance(schema['additionalProperties'], bool):
        raise ValueError('additionalProperties must be a boolean')
    if 'properties' in schema:
        if not isinstance(schema['properties'], dict):
            raise ValueError('properties must be an object')
        for sub in schema['properties'].values():
            check_schema_supported(sub, _depth + 1)
    if 'items' in schema:
        check_schema_supported(schema['items'], _depth + 1)


def _json_equal(a: Any, b: Any) -> bool:
    """Equality that does not treat True as 1."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return type(a) is type(b) and a == b


def _type_ok(value: Any, name: str) -> bool:
    if name == 'object':
        return isinstance(value, dict)
    if name == 'array':
        return isinstance(value, list)
    if name == 'string':
        return isinstance(value, str)
    if name == 'boolean':
        return isinstance(value, bool)
    if name == 'null':
        return value is None
    if name == 'integer':
        # JSON Schema: any number without a fractional part, so 1.0 and 1e0 count.
        if isinstance(value, float):
            return math.isfinite(value) and value.is_integer()
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, (int, float)) and not isinstance(value, bool)  # number


def validate_against_schema(value: Any, schema: Dict[str, Any]) -> bool:
    """
    True if ``value`` satisfies ``schema``. The schema must already have passed
    check_schema_supported(), so no keyword is silently skipped here.
    """
    declared = schema.get('type')
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(_type_ok(value, n) for n in names):
            return False
    if 'enum' in schema and not any(_json_equal(value, option) for option in schema['enum']):
        return False
    if isinstance(value, dict):
        props = schema.get('properties', {})
        if any(key not in value for key in schema.get('required', [])):
            return False
        if schema.get('additionalProperties', True) is False and any(k not in props for k in value):
            return False
        for key, sub in props.items():
            if key in value and not validate_against_schema(value[key], sub):
                return False
    if isinstance(value, list) and 'items' in schema:
        if not all(validate_against_schema(item, schema['items']) for item in value):
            return False
    return True


class CloudProviderMixin:
    """
    Defensive parsing of the shared BaseLLM numeric settings.

    BaseLLM reads about a dozen LLM_* numbers with int()/float(), so one typo
    (MAX_TOKENS=oops) would raise in the constructor, before the manager can
    degrade or fall back. The cloud providers route those reads through
    parse_number instead: a warning that names the setting, then the default.
    Other providers keep the strict behaviour.

    It also reads the explicit transport settings (proxy, CA bundle).
    """

    proxy: Optional[str]
    ca_bundle: Optional[str]
    transport_rejected: bool

    def _read_transport_settings(self, provider: str) -> None:
        """
        Load LLM_<PROVIDER>_PROXY and LLM_<PROVIDER>_CA_BUNDLE. An invalid or
        unreadable value is never used and never falls back to the environment:
        ``transport_rejected`` makes authenticate() fail closed with one error
        that names the setting (not its value).
        """
        self.proxy = cloud_setting(provider, 'proxy') or None
        self.ca_bundle = cloud_setting(provider, 'ca_bundle') or None
        self.transport_rejected = False
        if self.proxy and not validate_proxy_url(self.proxy):
            logger.error(
                f"✗ LLM_{provider}_PROXY rejected (needs an http(s) URL with a host, "
                "no credentials, query or fragment)")
            self.proxy, self.transport_rejected = None, True
        if self.ca_bundle and not validate_ca_bundle(self.ca_bundle):
            logger.error(f"✗ LLM_{provider}_CA_BUNDLE rejected (not a readable file)")
            self.ca_bundle, self.transport_rejected = None, True

    def _int_setting(self, key: str, default: int) -> int:
        return parse_number(
            f'LLM_{key.upper()}', get_config('LLM', key), default, integer=True, allow_zero=True)

    def _float_setting(self, key: str, default: float) -> float:
        return parse_number(
            f'LLM_{key.upper()}', get_config('LLM', key), default, allow_zero=True)
