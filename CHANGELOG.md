# Changelog

All notable changes to hypeman-social. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project uses
[semantic versioning](https://semver.org/) (0.x minors may break).

## [Unreleased]

## [0.2.0] — 2026-09-06

### Added
- **Threads support** (`ThreadsPlatform`): posting via the official Threads
  Graph API (container create + publish), 500-character limit with
  URL-preserving trims, link-preview attachment, reply threading, and a
  `test_connection()` probe. Plain HTTPS — no extra needed. Registered in
  `REGISTRY` as `'threads'`.
- **Starred-repository rendering** on every platform, ported from
  Star-Daemon's connectors: pass `event_kind: EVENT_STAR` and a `repo_data`
  dict (the GitHub/GitLab API repository object) in `stream_data`. Discord
  renders a rich embed (GitHub purple / GitLab orange / gold, repository +
  description + language + stars + forks fields, owner-avatar thumbnail);
  Bluesky builds an external card from the API metadata instead of scraping;
  Mastodon appends a text card and uploads the avatar with alt text; Matrix
  adds a "⭐ Starred on GitHub/GitLab" heading and repository paragraphs.
- **`LLMManager.generate_validated()`**: the generate → guardrails →
  stricter-retry flow every daemon reimplemented, now shared. Leniently
  ships the original message when the retry still has issues (style problems
  beat silence), with hard vetoes for profanity and duplicates.
- **PEP 561 typing**: the package now ships `py.typed`, and `get_config` has
  overloads so a string default provably returns a string. A mypy gate runs
  in CI.
- **Security workflows**: CodeQL analysis, OpenSSF Scorecard (with README
  badge), and Dependabot for both GitHub Actions SHAs and Python
  dependencies.
- **Documentation site**: mkdocs-material site published to GitHub Pages at
  chiefgyk3d.github.io/hypeman, built from the existing docs on every push
  to main.

## [0.1.1] — 2026-09-04

### Added
- Documentation suite: configuration reference, API reference, quickstart,
  design notes, contributing guide — linked from the README with absolute
  URLs so they work on the PyPI project page.

### Fixed
- PEP 639 license metadata: the wheel now carries
  `License-Expression: MPL-2.0`, so dependency scanners stop reporting
  "Unknown License".

## [0.1.0] — 2026-09-04

First release. Extracted from Boon-Tube-Daemon, stream-daemon, and
Star-Daemon, which carried three diverging copies of this code.

### Added
- **Social platforms** (`hypeman_social.social`): Bluesky (grapheme-aware
  300 limit, link/tag facets, link cards, threading), Mastodon (threading,
  thumbnail media with alt text), Discord (per-source webhooks and role
  mentions, rich embeds edited in place with live viewer counts, ended-state
  embeds), Matrix (HTML messages, password login with automatic token
  rotation, replies). `REGISTRY` for constructing every network generically;
  `safe_post()` never raises.
- **LLM layer** (`hypeman_social.llm`): Ollama and Gemini providers behind
  `LLMManager` with opt-in ordered failover (`LLM_FALLBACK_PROVIDER`),
  automatic reconnection of a downed provider, retry with exponential
  backoff and error classification, client-side Gemini rate limiting,
  thinking-mode support for reasoning models (explicit `think` parameter on
  capability-advertising Ollama models; extraction of usable posts from
  thinking text).
- **Guardrails** (`hypeman_social.llm.guardrails`): per-domain hallucination
  rejection via `ContentProfile` (`VIDEO_PROFILE`, `STREAM_PROFILE`,
  `STAR_PROFILE`, `GENERIC_PROFILE`), forbidden-word and profanity filters,
  exact hashtag-count enforcement, username-derived-hashtag stripping,
  platform-specific validation, quality scoring, cross-provider message
  deduplication, hashtag-safe trimming.
- **Config and secrets** (`hypeman_social.config`): layered lookup
  (Doppler → env → `.env` → default) for settings; manager-first chain
  (Doppler → AWS Secrets Manager → Vault → env) for credentials, so
  production secrets override stale local files.
- **Observability** (`hypeman_social.observability`): thread-safe
  `HealthState` with `/healthz` and `/status` HTTP endpoints (degraded LLM ≠
  unhealthy daemon), one-call logging setup with rotation, systemd
  detection, and repeat-message deduplication.
- **Packaging**: every network and provider is an optional extra; the
  package imports cleanly with zero extras and fails closed with install
  hints — enforced by a bare-install CI job. Python 3.9–3.13. Published to
  PyPI via Trusted Publishing (OIDC) on GitHub release.

[0.2.0]: https://github.com/ChiefGyk3D/hypeman/releases/tag/v0.2.0
[0.1.1]: https://github.com/ChiefGyk3D/hypeman/releases/tag/v0.1.1
[0.1.0]: https://github.com/ChiefGyk3D/hypeman/releases/tag/v0.1.0
