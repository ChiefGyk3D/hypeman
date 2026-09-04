# Contributing

## Development setup

```bash
git clone https://github.com/ChiefGyk3D/hypeman.git
cd hypeman
pip install -e ".[all,aws,vault,doppler,dev]"
```

## Before you push

CI runs exactly these; run them locally first:

```bash
ruff check hypeman_social tests     # lint (ruleset pinned in pyproject.toml)
pytest                              # full suite
pytest --cov=hypeman_social --cov-fail-under=65   # what the CI gate enforces
python -m build && twine check dist/*             # packaging sanity
```

Tests never touch the network — platforms and providers are exercised
against fakes. Keep it that way.

## The rules that aren't up for debate

These encode production incidents; see [docs/DESIGN.md](docs/DESIGN.md):

1. **The extras contract.** Every subpackage must import with only the core
   dependencies installed. A new third-party import gets a `try/except
   ImportError` guard, an entry in the matching extra in `pyproject.toml`,
   and a fail-closed `authenticate()` message. `tests/test_optional_extras.py`
   and the CI bare-install job enforce this.
2. **The availability contract.** No caller-visible `.enabled` gating;
   availability checks may heal. A failed `authenticate()` retains config
   so recovery has a target.
3. **`safe_post()` never raises. `generate()` returning None is normal.**
   Degradation must never become downtime.
4. **Hallucination checks reject outright.** Posting an invented detail is
   worse than posting nothing.

## Adding things

**A social network**: subclass `SocialPlatform` in
`hypeman_social/social/<name>.py` (guard any SDK import), implement
`authenticate()` and `post()`, add one line to `REGISTRY`, add the extra to
`pyproject.toml`, document its keys in `docs/CONFIGURATION.md`, and test
against a fake client (see `tests/test_bluesky_post.py` for the pattern).

**An LLM provider**: subclass `BaseLLM` in `hypeman_social/llm/<name>.py`,
implement `authenticate()` and `_raw_generate()` (raise on failure — the
base classifies and retries), register the name in
`manager.build_provider()`, add the extra, document, test.

**A content profile**: add a `ContentProfile` to `llm/profiles.py` with the
hallucination patterns your domain's models actually produce, and export it
from `llm/__init__.py`.

## Style

- Match the codebase: single quotes, ~100 columns, docstrings that say *why*.
- Comments record constraints and history, not narration of the next line.
- MPL-2.0 header on new files.

## Releasing

Maintainers only — the procedure lives in [docs/PUBLISHING.md](docs/PUBLISHING.md).
Short version: bump `version` in `pyproject.toml`, update `CHANGELOG.md`,
merge to `main`, publish a GitHub release with tag `vX.Y.Z`; Trusted
Publishing does the rest.
