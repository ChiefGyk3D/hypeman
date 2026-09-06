# Quickstart: Build an Announcement Daemon

hypeman-social handles the shouting — social publishing, AI message
generation with guardrails, config/secrets, health. You bring the thing
worth shouting about. This walkthrough builds a complete daemon in about
sixty lines.

## 1. Install

```bash
pip install "hypeman-social[all]"        # every network + both LLM providers
# or cherry-pick:
pip install "hypeman-social[bluesky,mastodon,ollama]"
```

Extras: `bluesky`, `mastodon`, `ollama`, `gemini`, `aws`, `vault`,
`doppler`, `all`, `dev`. Discord and Matrix need no extra — they're plain
HTTP. Everything degrades gracefully: a platform whose SDK isn't installed
fails closed at `authenticate()` with an install hint, never at import.

## 2. Configure

Create `.env` (full reference: [CONFIGURATION.md](CONFIGURATION.md)):

```bash
LLM_ENABLE=true
LLM_PROVIDER=ollama
LLM_OLLAMA_HOST=http://localhost
LLM_OLLAMA_MODEL=gemma3:4b
LLM_FALLBACK_PROVIDER=gemini      # optional; delete to stay fully local
GEMINI_API_KEY=...                # only if you kept the fallback

BLUESKY_ENABLE_POSTING=true
BLUESKY_HANDLE=you.bsky.social
BLUESKY_APP_PASSWORD=...

DISCORD_ENABLE_POSTING=true
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

## 3. Write the daemon

```python
"""blogd — announces new blog posts. The 'what' is yours; hypeman shouts it."""
import time

from hypeman_social.config import load_config
from hypeman_social.llm import LLMManager, GENERIC_PROFILE
from hypeman_social.observability import HealthState, start_health_server, configure_logging
from hypeman_social.social import REGISTRY

def check_for_new_post():
    """Your domain logic. Return (title, url) or None."""
    ...

def main():
    load_config()
    configure_logging()

    # Social platforms: construct everything, keep what authenticates.
    platforms = [cls() for cls in REGISTRY.values()]
    active = [p for p in platforms if p.authenticate()]

    # AI messages, with failover and guardrails. Optional: if LLM_ENABLE is
    # off or the server is down, you fall back to a template below.
    llm = LLMManager(profile=GENERIC_PROFILE)
    llm.authenticate()

    # Health endpoint: /healthz and /status on localhost:8080.
    health = HealthState('blogd')
    health.register('llm', llm.status)
    for p in active:
        health.register(p.name.lower(), p.status)
    start_health_server(health, port=8080)

    while True:
        llm.heartbeat()                   # notices LLM recovery on your schedule
        new = check_for_new_post()
        if new:
            title, url = new
            message = None
            if llm.is_available():
                raw = llm.generate(f'Write a short post announcing a new blog post titled "{title}". '
                                   f'No URL, no hashtags, under 200 characters.')
                if raw:
                    message, issues = llm.apply_guardrails(
                        raw, title=title, username='you', platform='generic',
                        char_limit=200)
            if not message:
                message = f'New post: {title}'   # template fallback — always have one
            message += f'\n\n{url}'

            for p in active:
                p.safe_post(message, stream_data={'title': title})  # never raises
            health.record_event('last_post', detail=title)

        health.record_event('last_poll')
        time.sleep(300)

if __name__ == '__main__':
    main()
```

That's a production-shaped daemon: multi-network posting, AI copy with
anti-hallucination guardrails, automatic LLM recovery, template fallback,
and a health endpoint — none of it written by you.

## The rules that keep it robust

1. **Always have a template fallback.** `generate()` returning None is a
   normal state (server rebooting, guardrails rejected the message), not an
   error. A missed announcement is worse than a boring one.
2. **Post with `safe_post()`, never `post()`.** One network's outage must not
   block the other networks or crash the loop.
3. **Never branch on `.enabled` — ask `is_available()`.** Availability checks
   are allowed to *heal* the connection; a boolean you cached at startup can
   only ever go stale. This rule exists because of a real outage
   ([DESIGN.md](DESIGN.md#the-availability-contract)).
4. **Call `heartbeat()` every poll cycle.** Then the first announcement after
   an outage uses AI instead of a template, because recovery was noticed
   before a post needed to go out.
5. **Pass `event_kind` in `stream_data`.** Don't make platforms guess what
   they're announcing ([API.md](API.md#event-kinds)).

## How the real daemons use it

Four projects run on this library today; each keeps only its domain logic
and voice:

| Daemon | Domain logic kept | hypeman provides |
|---|---|---|
| [Boon-Tube-Daemon](https://github.com/ChiefGyk3D/Boon-Tube-Daemon) | YouTube/TikTok polling, upload-announcement prompts (`VideoPostGenerator`) | every network, `VIDEO_PROFILE` guardrails, LLM failover, config |
| [Stream-Daemon](https://github.com/ChiefGyk3D/Stream-Daemon) | Twitch/YouTube/Kick live detection, go-live + thanks prompts, lenient strict-retry flow | networks, `STREAM_PROFILE`, provider plumbing, config |
| [Star-Daemon](https://github.com/ChiefGyk3D/Star-Daemon) | GitHub star polling, repo-explainer prompt (`StarAnnouncer`) | `STAR_PROFILE` anti-hallucination guardrails, LLM failover |
| [yomama-as-a-service](https://github.com/ChiefGyk3D/yomama-as-a-service) | Joke flavors, meanness that goes to eleven | LLM layer: Ollama + Gemini, retries, rate-limit handling |

The pattern in all four: a small generator class owning the prompts and the
domain shape of a post, holding an `LLMManager`, with the daemon's original
template path untouched as the fallback. Steal it.

## Running under Docker / systemd

- Logging auto-detects systemd and drops duplicate timestamps
  (journald already stamps).
- Health endpoint + Docker: `HEALTHCHECK CMD curl -f http://localhost:8080/healthz`.
- Secrets: prefer Doppler/AWS/Vault over baking `.env` into images —
  the secret chain already prefers managers over local files.
