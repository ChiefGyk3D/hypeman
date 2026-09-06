# hypeman-social

A hype man's entire job is announcing you loudly to a crowd. That's what this
library does: it's the shared core behind a family of daemons that shout about
your content on Bluesky, Mastodon, Discord, Matrix, and Threads.

| Daemon | Shouts when |
|---|---|
| [Boon-Tube-Daemon](https://github.com/ChiefGyk3D/Boon-Tube-Daemon) | you post a YouTube video or Short |
| [stream-daemon](https://github.com/ChiefGyk3D/stream-daemon) | you go live on Twitch, YouTube, or Kick |
| [Star-Daemon](https://github.com/ChiefGyk3D/Star-Daemon) | you star a GitHub repo |

## Install

```bash
pip install "hypeman-social[all]"
```

The distribution is `hypeman-social` and the import is `hypeman_social`.
Every network and LLM backend is an optional extra: `bluesky`, `mastodon`,
`ollama`, `gemini`, `aws`, `vault`, `doppler`, `all`, `dev`. Discord, Matrix,
and Threads need no extra — they're plain HTTP.

## Where to go

- **[Quickstart](QUICKSTART.md)** — build a complete announcement daemon in ~60 lines.
- **[Configuration reference](CONFIGURATION.md)** — every env var, with defaults and worked examples.
- **[API reference](API.md)** — the full public surface, module by module.
- **[Design notes](DESIGN.md)** — why it's shaped this way: the availability contract, opt-in failover, guardrail philosophy.
- **[Publishing](PUBLISHING.md)** — PyPI Trusted Publishing setup and the release procedure.
- [Contributing](https://github.com/ChiefGyk3D/hypeman/blob/main/CONTRIBUTING.md) and the [changelog](https://github.com/ChiefGyk3D/hypeman/blob/main/CHANGELOG.md) live in the repository.

## The one rule

**Never branch on `.enabled`. Always call `is_available()`.**

`is_available()` is allowed to *heal*: if a provider is down but recoverable,
it attempts a cooldown-guarded reconnect and reports the result. A gate that
can only ever go from working to broken is not a health check, it's a latch —
that latch once cost a daemon its AI until someone noticed and restarted the
process, and this library exists so that never happens again. The full story
is in the [design notes](DESIGN.md).
