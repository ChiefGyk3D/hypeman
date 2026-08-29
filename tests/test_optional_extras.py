# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
The optional-extras contract: hypeman-social installs with only its core
dependencies (python-dotenv, requests), and every subpackage must import
cleanly in that environment. A daemon that only posts to Discord should not
need atproto, Mastodon.py, ollama, or google-genai on disk.

These tests run the imports in a subprocess where the optional packages are
shadowed by stubs that raise ImportError, which is exactly what a bare
install looks like.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# Every distribution named by an extra in pyproject.toml, by import name.
OPTIONAL_MODULES = [
    'atproto',
    'grapheme',
    'mastodon',
    'ollama',
    'google',       # google-genai lives under the google namespace
    'boto3',
    'hvac',
    'dopplersdk',
]


def _run_with_extras_blocked(code: str) -> subprocess.CompletedProcess:
    """Run python code in a subprocess where all optional extras fail to import."""
    blocker = textwrap.dedent(f"""
        import sys

        class _ExtrasBlocker:
            BLOCKED = {OPTIONAL_MODULES!r}

            def find_module(self, fullname, path=None):
                root = fullname.split('.')[0]
                if root in self.BLOCKED:
                    return self
                return None

            # Py3.9-compatible finder protocol
            def find_spec(self, fullname, path=None, target=None):
                root = fullname.split('.')[0]
                if root in self.BLOCKED:
                    raise ImportError(f"{{fullname}} blocked: simulating bare install")
                return None

        sys.meta_path.insert(0, _ExtrasBlocker())
    """)
    repo_root = Path(__file__).resolve().parent.parent
    return subprocess.run(
        [sys.executable, '-c', blocker + '\n' + textwrap.dedent(code)],
        check=False,
        capture_output=True,
        text=True,
        cwd=repo_root,
        timeout=60,
    )


@pytest.mark.parametrize('module', [
    'hypeman_social',
    'hypeman_social.config',
    'hypeman_social.social',
    'hypeman_social.llm',
    'hypeman_social.observability',
])
def test_subpackage_imports_without_extras(module):
    result = _run_with_extras_blocked(f"import {module}")
    assert result.returncode == 0, (
        f"`import {module}` failed on a bare install:\n{result.stderr}"
    )


def test_registry_platforms_construct_without_extras():
    """Constructing any platform (not using it) must work on a bare install."""
    result = _run_with_extras_blocked("""
        from hypeman_social.social import REGISTRY
        for name, cls in REGISTRY.items():
            instance = cls()
            assert instance.name.lower() == name, name
    """)
    assert result.returncode == 0, result.stderr


def test_missing_extra_fails_closed_at_authenticate():
    """
    With the network enabled in config but the SDK absent, authenticate()
    must return False (and log) rather than raise.
    """
    result = _run_with_extras_blocked("""
        import os
        os.environ['BLUESKY_ENABLE_POSTING'] = 'true'
        os.environ['MASTODON_ENABLE_POSTING'] = 'true'

        from hypeman_social.social.bluesky import BlueskyPlatform
        from hypeman_social.social.mastodon import MastodonPlatform

        assert BlueskyPlatform().authenticate() is False
        assert MastodonPlatform().authenticate() is False
    """)
    assert result.returncode == 0, result.stderr


def test_llm_providers_fail_closed_without_extras():
    """OllamaLLM/GeminiLLM authenticate() must return False, not raise."""
    result = _run_with_extras_blocked("""
        from hypeman_social.llm.ollama import OllamaLLM
        from hypeman_social.llm.gemini import GeminiLLM

        assert OllamaLLM().authenticate() is False
        assert GeminiLLM().authenticate() is False
    """)
    assert result.returncode == 0, result.stderr
