# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Configuration helpers shared by every Hypeman-powered daemon.

Reads from Doppler (if DOPPLER_TOKEN is set), then environment variables,
then .env, then whatever default you passed in. Two env key formats are
supported so both the simple and sectioned styles keep working:

    Simple:    CHECK_INTERVAL
    Sectioned: SETTINGS_CHECK_INTERVAL

Config management is the plumbing nobody brags about. It's the pipes under
the sink. Nobody compliments your pipes. They only notice when shit leaks.
"""

import logging
import os
from pathlib import Path
from typing import Any, List, Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Track whether we've already loaded a .env so repeat calls are cheap no-ops.
_env_loaded = False

# Values starting with this prefix are template placeholders, not real config.
_PLACEHOLDER_PREFIX = 'YOUR_'


def _is_placeholder(value: Any) -> bool:
    """True if the value is an unedited template placeholder like YOUR_API_KEY."""
    return isinstance(value, str) and value.startswith(_PLACEHOLDER_PREFIX)


def load_config(env_path: str = ".env") -> bool:
    """
    Load configuration from a .env file.

    Args:
        env_path: Path to the .env file.

    Returns:
        True if a file was found and loaded, False otherwise.
    """
    global _env_loaded

    if _env_loaded:
        return True

    env_file = Path(env_path)
    if env_file.exists():
        load_dotenv(env_file)
        logger.info(f"✓ Loaded configuration from {env_path}")
        _env_loaded = True
        return True

    logger.warning(f"⚠ Configuration file not found: {env_path}")
    return False


def _doppler_lookup(*candidate_keys: str) -> Optional[str]:
    """
    Look up the first matching key in Doppler.

    Returns None if Doppler isn't configured, isn't installed, or has no match.
    Never raises — Doppler being down should degrade to env vars, not crash the daemon.
    """
    if not os.getenv('DOPPLER_TOKEN'):
        return None

    try:
        from dopplersdk import DopplerSDK

        sdk = DopplerSDK(access_token=os.getenv('DOPPLER_TOKEN'))
        response = sdk.secrets.list(
            project=os.getenv('DOPPLER_PROJECT'),
            config=os.getenv('DOPPLER_CONFIG', 'dev'),
        )

        secrets = getattr(response, 'secrets', None)
        if not secrets:
            return None

        for candidate in candidate_keys:
            if candidate in secrets:
                entry = secrets[candidate]
                value = entry.get('computed', entry.get('raw', ''))
                if value and not _is_placeholder(value):
                    logger.debug(f"✓ Retrieved {candidate} from Doppler")
                    return value
    except ImportError:
        logger.debug("dopplersdk not installed, skipping Doppler lookup")
    except Exception as e:
        logger.debug(f"Doppler lookup failed: {type(e).__name__}")

    return None


def get_config(section: str, key: str, default: Any = None) -> Optional[str]:
    """
    Get a configuration value.

    Priority: Doppler -> simple env key -> sectioned env key -> default.
    An empty string is treated as "not set" and falls through to the default.

    Args:
        section: Config section, e.g. 'Twitch', 'YouTube', 'Settings'.
        key: Config key, e.g. 'username', 'api_key', 'check_interval'.
        default: Value to return when nothing else matches.

    Returns:
        The configuration value, or the default.
    """
    simple_key = key.upper()
    sectioned_key = f"{section}_{key}".upper()

    value = _doppler_lookup(sectioned_key, simple_key)
    if value:
        return value

    # Simple format first (CHECK_INTERVAL), then sectioned (SETTINGS_CHECK_INTERVAL).
    # Empty strings count as unset so a blank .env line doesn't override a default.
    for env_key in (simple_key, sectioned_key):
        value = os.getenv(env_key)
        if value:
            return value

    logger.debug(f"Config not found: {section}.{key} (tried {simple_key}, {sectioned_key})")
    return default


def get_bool_config(section: str, key: str, default: bool = False) -> bool:
    """Get a boolean config value. Accepts true/1/yes/on/enabled, case-insensitive."""
    value = get_config(section, key)

    if value is None:
        return default
    if isinstance(value, bool):
        return value

    return str(value).strip().lower() in ('true', '1', 'yes', 'on', 'enabled')


def get_int_config(section: str, key: str, default: int = 0) -> int:
    """Get an integer config value, falling back to the default if unparseable."""
    value = get_config(section, key)

    if value is None:
        return default

    try:
        return int(value)
    except (ValueError, TypeError):
        logger.warning(f"Invalid integer for {section}.{key}, using default: {default}")
        return default


def get_float_config(section: str, key: str, default: float = 0.0) -> float:
    """Get a float config value, falling back to the default if unparseable."""
    value = get_config(section, key)

    if value is None:
        return default

    try:
        return float(value)
    except (ValueError, TypeError):
        logger.warning(f"Invalid float for {section}.{key}, using default: {default}")
        return default


def get_usernames(section: str, default: Any = None) -> List[str]:
    """
    Get a list of usernames for a platform.

    Accepts both the plural (SECTION_USERNAMES) and singular (SECTION_USERNAME)
    forms, comma-separated for multiple accounts.

        TWITCH_USERNAME=user1               -> ['user1']
        TWITCH_USERNAMES=user1,user2        -> ['user1', 'user2']
        YOUTUBE_USERNAME=@one, @two         -> ['@one', '@two']

    Args:
        section: Platform section, e.g. 'Twitch', 'YouTube', 'Kick'.
        default: Fallback when nothing is configured. String or list.

    Returns:
        A list of usernames, empty if none are configured.
    """
    try:
        raw = os.getenv(f'{section.upper()}_USERNAMES') or os.getenv(f'{section.upper()}_USERNAME')

        if not raw:
            if default is None:
                return []
            return list(default) if isinstance(default, list) else [default]

        return [u.strip() for u in raw.split(',') if u.strip()]
    except Exception as e:
        logger.error(f"Error getting usernames for {section}: {type(e).__name__}")
        if default is None:
            return []
        return list(default) if isinstance(default, list) else [default]
