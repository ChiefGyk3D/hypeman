# hypeman-social

A hype man's entire job is announcing you loudly to a crowd. That's what this
library does: it's the shared core behind a family of daemons that shout about
your content on Bluesky, Mastodon, Discord, and Matrix.

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

Discord and Matrix need no extra — they're plain HTTP.

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
hypeman_social.social          Bluesky, Mastodon, Discord, Matrix
hypeman_social.observability   logging with rotation, health endpoints
```

Nothing in here knows what you're announcing. That's the caller's business:
daemons own their own prompts, polling, and state.

## Adding a social network

Write the module, subclass `SocialPlatform`, add one line to `REGISTRY` in
`hypeman/social/__init__.py`. Every daemon picks it up. (Threads is next.)

## Development

```bash
pip install -e ".[all,dev]"
pytest
```

## License

MPL-2.0
