# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Secret retrieval across AWS Secrets Manager, HashiCorp Vault, and Doppler.

Because storing a Twitch API key in a text file was too simple. We need THREE
enterprise secret management systems with fallback chains. Nothing says "I take
my Minecraft stream seriously" like HashiCorp Vault integration.

All three backends are OPTIONAL. Their SDKs are imported lazily, so a daemon
that just uses a .env file doesn't need boto3, hvac, or dopplersdk installed.
"""

import json
import logging
import os
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_PLACEHOLDER_PREFIX = 'YOUR_'

# Doppler hands back every secret in a project/config in one call, but the old
# code re-fetched that whole list for EVERY credential lookup. With ~20
# credentials across eight platforms that is 20+ API calls during startup,
# which is slow and reliably trips Doppler's rate limit. The resulting failure
# surfaces as "missing credentials" rather than "too many requests", which is a
# genuinely confusing way to lose an afternoon.
#
# One fetch per process. Call reset_secret_cache() to force a re-read.
_doppler_cache = None
_doppler_cache_lock = threading.Lock()


def reset_secret_cache() -> None:
    """Forget cached secrets so the next lookup re-fetches them."""
    global _doppler_cache
    with _doppler_cache_lock:
        _doppler_cache = None


def _doppler_secrets() -> Dict[str, Any]:
    """
    Every secret in the configured Doppler project, fetched once per process.

    Returns an empty dict — and caches that — when Doppler is unreachable, so a
    rate limit or outage degrades to environment variables instead of stalling
    startup behind repeated failing requests.
    """
    global _doppler_cache

    if _doppler_cache is not None:
        return _doppler_cache

    with _doppler_cache_lock:
        if _doppler_cache is not None:
            return _doppler_cache

        try:
            from dopplersdk import DopplerSDK
        except ImportError:
            logger.debug("dopplersdk not installed, skipping Doppler")
            _doppler_cache = {}
            return _doppler_cache

        try:
            sdk = DopplerSDK()
            sdk.set_access_token(os.getenv('DOPPLER_TOKEN'))
            response = sdk.secrets.list(
                project=os.getenv('DOPPLER_PROJECT'),
                config=os.getenv('DOPPLER_CONFIG', 'prd'),
            )

            secrets = getattr(response, 'secrets', None) or {}
            _doppler_cache = {
                key: entry.get('computed', entry.get('raw', ''))
                for key, entry in secrets.items()
            }
            logger.debug(f"Loaded {len(_doppler_cache)} secrets from Doppler")
        except Exception as e:
            logger.error(f"Failed to fetch Doppler secrets: {type(e).__name__}")
            _doppler_cache = {}

        return _doppler_cache




def _usable(value: Any) -> bool:
    """True if the value is present and isn't an unedited template placeholder."""
    return bool(value) and not (isinstance(value, str) and value.startswith(_PLACEHOLDER_PREFIX))


def load_secrets_from_aws(secret_name: str) -> Dict[str, Any]:
    """
    Load a secret bundle from AWS Secrets Manager.

    Because Jeff Bezos should definitely know about your Twitch announcements.

    Args:
        secret_name: The SecretId in AWS Secrets Manager.

    Returns:
        Dict of secrets, or an empty dict on any error.
    """
    try:
        import boto3
    except ImportError:
        logger.debug("boto3 not installed, skipping AWS Secrets Manager")
        return {}

    try:
        client = boto3.client('secretsmanager')
        response = client.get_secret_value(SecretId=secret_name)
        logger.debug("Successfully loaded AWS secret")
        return json.loads(response['SecretString'])
    except Exception as e:
        logger.error(f"Failed to load AWS secret: {type(e).__name__}")
        return {}


def load_secrets_from_vault(secret_path: str) -> Dict[str, Any]:
    """
    Load a secret bundle from HashiCorp Vault (KV v2).

    Args:
        secret_path: Path to the secret within the KV mount.

    Returns:
        Dict of secrets, or an empty dict on any error.
    """
    try:
        import hvac
    except ImportError:
        logger.debug("hvac not installed, skipping HashiCorp Vault")
        return {}

    try:
        vault_url = os.getenv('SECRETS_VAULT_URL')
        vault_token = os.getenv('SECRETS_VAULT_TOKEN')

        if not vault_url or not vault_token:
            logger.error("Vault URL or token not configured")
            return {}

        client = hvac.Client(url=vault_url, token=vault_token)
        if not client.is_authenticated():
            logger.error("Vault authentication failed")
            return {}

        response = client.secrets.kv.v2.read_secret_version(path=secret_path)
        logger.debug("Successfully loaded Vault secret")
        return response['data']['data']
    except Exception as e:
        logger.error(f"Failed to load Vault secret: {type(e).__name__}")
        return {}


def load_secrets_from_doppler(secret_name: str) -> Dict[str, Any]:
    """
    Load secrets from Doppler, filtered to a platform prefix.

    Given secret_name='twitch', returns every Doppler secret starting with
    TWITCH_, re-keyed without the prefix: TWITCH_CLIENT_ID -> 'client_id'.

    Args:
        secret_name: Platform prefix, e.g. 'twitch', 'youtube'.

    Returns:
        Dict of prefix-stripped secrets, empty on any error.
    """
    if not os.getenv('DOPPLER_TOKEN'):
        logger.debug("DOPPLER_TOKEN not set")
        return {}

    prefix = f"{secret_name.upper()}_"
    bundle = {
        key[len(prefix):].lower(): value
        for key, value in _doppler_secrets().items()
        if key.upper().startswith(prefix)
    }

    if not bundle:
        logger.debug(f"No Doppler secrets found with prefix {prefix}")

    return bundle


def _doppler_direct_key(key: str) -> Optional[str]:
    """
    Fetch an unprefixed key straight out of Doppler.

    Some secrets (GEMINI_API_KEY) live in Doppler without a platform prefix, so
    the prefix-filtered bundle above misses them.
    """
    value = _doppler_secrets().get(key.upper())
    return value if _usable(value) else None


def _lookup_in_bundle(bundle: Dict[str, Any], key: str, platform: str) -> Optional[str]:
    """
    Find a key in a secret bundle, tolerating the naming styles backends use.

    Tries 'client_id', 'CLIENT_ID', then 'TWITCH_CLIENT_ID'.
    """
    if not bundle:
        return None

    for candidate in (key, key.upper(), f"{platform.upper()}_{key.upper()}"):
        value = bundle.get(candidate)
        if _usable(value):
            return value

    return None


def get_secret(
    platform: str,
    key: str,
    default: Optional[str] = None,
    secret_name_env: Optional[str] = None,
    secret_path_env: Optional[str] = None,
    doppler_secret_env: Optional[str] = None,
) -> Optional[str]:
    """
    Get a secret, preferring a secrets manager over local .env values.

    Priority:
      1. Doppler                     (if DOPPLER_TOKEN is set)
      2. AWS Secrets Manager         (if SECRETS_MANAGER=aws)
      3. HashiCorp Vault             (if SECRETS_MANAGER=vault)
      4. Environment variable / .env (PLATFORM_KEY)
      5. The supplied default

    Production secrets in a manager therefore override local .env defaults,
    which is what you want on a real deployment.

    The *_env arguments name the ENV VAR holding the secret's location in each
    backend. They're optional; when omitted, hypeman falls back to a
    conventional `<platform>` bundle name.

    Args:
        platform: Platform name, e.g. 'Twitch', 'Bluesky'.
        key: Secret key, e.g. 'client_id', 'app_password'.
        default: Value returned when nothing else matches.
        secret_name_env: Env var naming the AWS secret, e.g. SECRETS_AWS_BLUESKY_SECRET_NAME.
        secret_path_env: Env var naming the Vault path, e.g. SECRETS_VAULT_BLUESKY_SECRET_PATH.
        doppler_secret_env: Env var naming the Doppler prefix, e.g. SECRETS_DOPPLER_BLUESKY_SECRET_NAME.

    Returns:
        The secret value, or the default if not found.
    """
    try:
        # 1. Doppler, auto-detected via DOPPLER_TOKEN.
        #
        # Note the ordering: the Doppler API is consulted BEFORE the local
        # environment, so a production secret genuinely overrides a stale .env
        # value. Users running under `doppler run` still get their injected
        # values — those arrive as plain env vars and are picked up by step 4.
        # Checking the env first here would quietly invert that priority.
        if os.getenv('DOPPLER_TOKEN'):
            prefix = os.getenv(doppler_secret_env) if doppler_secret_env else platform.lower()
            if prefix:
                value = load_secrets_from_doppler(prefix).get(key)
                if _usable(value):
                    logger.debug(f"✓ Retrieved {platform}.{key} from Doppler")
                    return value

            # Unprefixed keys like GEMINI_API_KEY live at the top level.
            value = _doppler_direct_key(key)
            if value:
                logger.debug(f"✓ Retrieved {platform}.{key} from Doppler (direct key)")
                return value

        manager = os.getenv('SECRETS_MANAGER', 'none').lower()

        # 2. AWS Secrets Manager.
        if manager == 'aws':
            name = os.getenv(secret_name_env) if secret_name_env else platform.lower()
            if name:
                value = _lookup_in_bundle(load_secrets_from_aws(name), key, platform)
                if value:
                    logger.debug(f"✓ Retrieved {platform}.{key} from AWS Secrets Manager")
                    return value

        # 3. HashiCorp Vault.
        elif manager == 'vault':
            path = os.getenv(secret_path_env) if secret_path_env else platform.lower()
            if path:
                value = _lookup_in_bundle(load_secrets_from_vault(path), key, platform)
                if value:
                    logger.debug(f"✓ Retrieved {platform}.{key} from HashiCorp Vault")
                    return value

        # 4. Plain environment variable / .env.
        env_value = os.getenv(f"{platform.upper()}_{key.upper()}")
        if _usable(env_value):
            logger.debug(f"✓ Retrieved {platform}.{key} from environment/.env")
            return env_value

        if env_value:
            logger.warning(
                f"⚠ {platform}.{key} is still set to a template placeholder "
                f"(starts with '{_PLACEHOLDER_PREFIX}') — edit your .env"
            )

        logger.debug(f"Secret not found: {platform}.{key}")
        return default

    except Exception as e:
        logger.error(f"Error getting secret {platform}.{key}: {type(e).__name__}")
        return default
