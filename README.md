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

---

## 💝 Support This Project

If you find hypeman-social useful, consider supporting continued development.
Everything is also collected at **[support.chiefgyk3d.com](https://support.chiefgyk3d.com)**.

### Recurring Support

<div align="center">
<table>
  <tr>
    <td align="center" width="150">
      <a href="https://patreon.com/chiefgyk3d" title="Patreon">
        <img src="media/icons/patreon.svg" width="36" height="36" alt="Patreon"><br>
        <sub><b>Patreon</b></sub>
      </a>
    </td>
    <td align="center" width="150">
      <a href="https://streamelements.com/chiefgyk3d/tip" title="StreamElements">
        <img src="media/streamelements.png" width="36" height="36" alt="StreamElements"><br>
        <sub><b>StreamElements</b></sub>
      </a>
    </td>
    <td align="center" width="150">
      <a href="https://shop.chiefgyk3d.com/" title="Merch Store">
        <img src="media/icons/merch.svg" width="36" height="36" alt="Merch"><br>
        <sub><b>Merch Store</b></sub>
      </a>
    </td>
  </tr>
</table>
</div>

### Cryptocurrency Tips

<div align="center">
<table>
  <tr>
    <td align="center" width="60"><img src="media/icons/bitcoin.svg" width="28" height="28" alt="Bitcoin"></td>
    <td><b>Bitcoin</b><br><code>bc1qztdzcy2wyavj2tsuandu4p0tcklzttvdnzalla</code></td>
  </tr>
  <tr>
    <td align="center" width="60"><img src="media/icons/monero.svg" width="28" height="28" alt="Monero"></td>
    <td><b>Monero</b><br><code>84Y34QubRwQYK2HNviezeH9r6aRcPvgWmKtDkN3EwiuVbp6sNLhm9ffRgs6BA9X1n9jY7wEN16ZEpiEngZbecXseUrW8SeQ</code></td>
  </tr>
  <tr>
    <td align="center" width="60"><img src="media/icons/ethereum.svg" width="28" height="28" alt="Ethereum"></td>
    <td><b>Ethereum</b><br><code>0x554f18cfB684889c3A60219BDBE7b050C39335ED</code></td>
  </tr>
  <tr>
    <td align="center" width="60"><img src="media/icons/solana.svg" width="28" height="28" alt="Solana"></td>
    <td><b>Solana</b><br><code>5T8h3HbyvHgLxwXgchRYbHSqRjZyAr8J7uwjLN9Fh8Jh</code></td>
  </tr>
</table>
</div>

---

## 👤 Author & Socials

<div align="center">
<table>
  <tr>
    <td align="center" width="90"><a href="https://social.chiefgyk3d.com/@chiefgyk3d" title="Mastodon"><img src="media/icons/mastodon.svg" width="30" height="30" alt="Mastodon"><br><sub>Mastodon</sub></a></td>
    <td align="center" width="90"><a href="https://bsky.app/profile/chiefgyk3d.com" title="Bluesky"><img src="media/icons/bluesky.svg" width="30" height="30" alt="Bluesky"><br><sub>Bluesky</sub></a></td>
    <td align="center" width="90"><a href="https://twitch.tv/chiefgyk3d" title="Twitch"><img src="media/icons/twitch.svg" width="30" height="30" alt="Twitch"><br><sub>Twitch</sub></a></td>
    <td align="center" width="90"><a href="https://www.youtube.com/channel/UCvFY4KyqVBuYd7JAl3NRyiQ" title="YouTube"><img src="media/icons/youtube.svg" width="30" height="30" alt="YouTube"><br><sub>YouTube</sub></a></td>
    <td align="center" width="90"><a href="https://kick.com/chiefgyk3d" title="Kick"><img src="media/icons/kick.svg" width="30" height="30" alt="Kick"><br><sub>Kick</sub></a></td>
    <td align="center" width="90"><a href="https://www.tiktok.com/@chiefgyk3d" title="TikTok"><img src="media/icons/tiktok.svg" width="30" height="30" alt="TikTok"><br><sub>TikTok</sub></a></td>
    <td align="center" width="90"><a href="https://discord.chiefgyk3d.com" title="Discord"><img src="media/icons/discord.svg" width="30" height="30" alt="Discord"><br><sub>Discord</sub></a></td>
    <td align="center" width="90"><a href="https://matrix-invite.chiefgyk3d.com" title="Matrix"><img src="media/icons/matrix.svg" width="30" height="30" alt="Matrix"><br><sub>Matrix</sub></a></td>
  </tr>
</table>
</div>

<div align="center"><sub>Made with ❤️ by <a href="https://github.com/ChiefGyk3D">ChiefGyk3D</a></sub></div>
