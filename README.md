# hypeman-social

[![CI](https://github.com/ChiefGyk3D/hypeman/actions/workflows/ci.yml/badge.svg)](https://github.com/ChiefGyk3D/hypeman/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/hypeman-social)](https://pypi.org/project/hypeman-social/)
[![Python versions](https://img.shields.io/pypi/pyversions/hypeman-social)](https://pypi.org/project/hypeman-social/)
[![License: MPL 2.0](https://img.shields.io/badge/License-MPL_2.0-brightgreen.svg)](https://opensource.org/licenses/MPL-2.0)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/ChiefGyk3D/hypeman/badge)](https://scorecard.dev/viewer/?uri=github.com/ChiefGyk3D/hypeman)
[![Docs](https://img.shields.io/badge/docs-site-blue)](https://chiefgyk3d.github.io/hypeman/)

A hype man's entire job is announcing you loudly to a crowd. That's what this
library does: it's the shared core behind a family of daemons that shout about
your content on Bluesky, Mastodon, Discord, Matrix, and Threads.

| Daemon | Shouts when |
|---|---|
| [Boon-Tube-Daemon](https://github.com/ChiefGyk3D/Boon-Tube-Daemon) | you post a YouTube video or Short |
| [stream-daemon](https://github.com/ChiefGyk3D/stream-daemon) | you go live on Twitch, YouTube, or Kick |
| [Star-Daemon](https://github.com/ChiefGyk3D/Star-Daemon) | you star a GitHub repo |

## Why this exists

These daemons were three copies of the same code, drifting apart. Roughly 55-65%
of the social-publishing code was identical line-for-line, and the LLM layer
shared 352 identical lines across two files that had already diverged.

That duplication caused a real outage. An auto-reconnect fix for a local Ollama
server was written for one daemon and never reached the other — and in the
daemon that *had* it, the fix was unreachable anyway, because every call site
gated on an `enabled` flag that the failure path set to `False` and nothing
ever set back. Taking the AI server offline meant no AI until someone noticed
and restarted the process.

A fix that lands in one repo should protect all of them. That's what this is for.

## Install

> **Name note:** the distribution is `hypeman-social` and the import is
> `hypeman_social`. Plain `hypeman` on PyPI is an unrelated project that ships
> its own top-level `hypeman` module, so this package deliberately avoids that
> name — installing both would otherwise break one of them silently.

```bash
pip install hypeman-social[all]
```

Every network and LLM backend is an optional extra, so install only what you use:

```bash
pip install hypeman-social[bluesky,mastodon,ollama]
```

Extras: `bluesky`, `mastodon`, `ollama`, `gemini`, `aws`, `vault`, `doppler`, `all`, `dev`.

Discord, Matrix, and Threads need no extra — they're plain HTTP.

## Documentation

The same docs are browsable as a site at
[chiefgyk3d.github.io/hypeman](https://chiefgyk3d.github.io/hypeman/).

| Doc | What's in it |
|---|---|
| [Quickstart](https://github.com/ChiefGyk3D/hypeman/blob/main/docs/QUICKSTART.md) | Build a complete announcement daemon in ~60 lines |
| [Configuration reference](https://github.com/ChiefGyk3D/hypeman/blob/main/docs/CONFIGURATION.md) | Every env var, with defaults and worked examples |
| [API reference](https://github.com/ChiefGyk3D/hypeman/blob/main/docs/API.md) | The full public surface, module by module |
| [Design notes](https://github.com/ChiefGyk3D/hypeman/blob/main/docs/DESIGN.md) | Why it's shaped this way — the availability contract, opt-in failover, guardrail philosophy |
| [Publishing](https://github.com/ChiefGyk3D/hypeman/blob/main/docs/PUBLISHING.md) | PyPI Trusted Publishing setup and release procedure |
| [Contributing](https://github.com/ChiefGyk3D/hypeman/blob/main/CONTRIBUTING.md) | Dev setup, the non-negotiable contracts, how to add platforms/providers |
| [Changelog](https://github.com/ChiefGyk3D/hypeman/blob/main/CHANGELOG.md) | Release history |

## The availability contract

**Never branch on `.enabled`. Always call `is_available()`.**

This is the entire lesson of the outage. `is_available()` is allowed to *heal*:
if the provider is down but recoverable, it attempts a cooldown-guarded
reconnect and reports the result. A gate that can only ever go from working to
broken is not a health check, it's a latch.

```python
from hypeman_social.llm import LLMManager, VIDEO_PROFILE

llm = LLMManager(profile=VIDEO_PROFILE)
llm.authenticate()

# Correct: this can recover a downed server.
if llm.is_available():
    message = llm.generate(prompt)

# Wrong: this is the bug. It can never come back.
# if llm.primary.enabled: ...
```

A failed `authenticate()` at startup is **not** fatal. Configuration is retained,
and the daemon keeps trying in the background, so starting up while your AI box
is offline costs you some template-fallback posts rather than a broken process.

## Provider failover

Ollama and Gemini have complementary failure modes — your local box goes down
for power cuts and GPU driver updates, Gemini goes down for rate limits and
outages. They rarely go down together.

```bash
LLM_PROVIDER=ollama
LLM_FALLBACK_PROVIDER=gemini   # opt-in
```

Failover is **opt-in by design**. If you chose Ollama specifically so your data
stays on your network, silently shipping prompts to Google would be a betrayal,
not a feature. When the primary recovers, hypeman switches back automatically.

## Validated generation in one call

```python
message = llm.generate_validated(
    lambda strict: build_prompt(title, strict_mode=strict),
    title=title, username='chief', platform='bluesky',
    char_limit=300, expected_hashtags=3,
)
```

Generate, run the guardrails, and retry once with a stricter prompt when the
first attempt has issues. If the retry still isn't clean, the original ships
anyway — minor style problems beat silence — except for two hard vetoes:
profanity (when the filter is on) and duplicates of recent posts. Every
daemon used to reimplement this loop; now a fix to it reaches all of them.

## Content profiles

The same guardrails apply everywhere, but the vocabulary differs. A stream
announcement promising "VOD coming soon" is a hallucination; so is a video
announcement claiming you're "live now". Rather than fork the guardrails, pass
a profile:

```python
from hypeman_social.llm.profiles import VIDEO_PROFILE, STREAM_PROFILE, ContentProfile
```

Define your own `ContentProfile` for anything else you're announcing.

## Logging

Configurable level, optional rotating file output, and journald-aware
formatting. Under systemd, timestamps are omitted automatically because
journald already stamps every line.

```bash
LOG_LEVEL=INFO
LOG_FILE=/var/log/stream-daemon/daemon.log   # enables rotation
LOG_MAX_BYTES=10485760                       # 10 MB
LOG_BACKUP_COUNT=5
LOG_DEDUPE_SECONDS=300                       # collapse repeated poll lines
```

`LOG_DEDUPE_SECONDS` is the one to reach for if your logs are enormous. A daemon
polling every two minutes writes ~720 identical "nothing happening" lines a day;
this collapses them and reports the count. Warnings and errors are never
suppressed.

```python
from hypeman_social.observability import configure_logging
configure_logging()
```

## Health

```bash
HEALTH_PORT=9101
```

- `GET /healthz` — 200 alive, 503 something essential is broken
- `GET /status` — full detail: every platform, the LLM, last poll, last post, uptime

A downed AI server reports as **degraded, not unhealthy** — the daemon is still
doing its job with fallback messages, and a health check that cries wolf gets
ignored.

Binds to `127.0.0.1` by default. The endpoint reports which platforms are
configured and whether credentials work; expose it publicly only behind
something that controls who can reach it.

## Layout

```
hypeman_social.config          config + secrets (env, .env, AWS, Vault, Doppler)
hypeman_social.llm             Ollama + Gemini, guardrails, failover manager
hypeman_social.social          Bluesky, Mastodon, Discord, Matrix, Threads
hypeman_social.observability   logging with rotation, health endpoints
```

Nothing in here knows what you're announcing. That's the caller's business:
daemons own their own prompts, polling, and state.

## Adding a social network

Write the module, subclass `SocialPlatform`, add one line to `REGISTRY` in
`hypeman_social/social/__init__.py`. Every daemon picks it up — that's
exactly how Threads landed in 0.2.0. Full checklist — extras guard, config docs, fake-client tests — in
[CONTRIBUTING.md](https://github.com/ChiefGyk3D/hypeman/blob/main/CONTRIBUTING.md#adding-things).

## Development

```bash
pip install -e ".[all,aws,vault,doppler,dev]"
ruff check hypeman_social tests
pytest
```

CI runs the same lint and tests across Python 3.9–3.13, plus a mypy
type-check (the package ships `py.typed`), a bare-install job (the package
must work with zero extras), a coverage gate, and a build + `twine check` of
the sdist and wheel. CodeQL and OpenSSF Scorecard run on every push to main,
and Dependabot keeps the SHA-pinned actions and dependency floors current.

## License

MPL-2.0
