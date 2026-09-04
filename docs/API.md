# API Reference

The public surface of `hypeman_social`, module by module. Anything prefixed
with `_` is internal and may change without notice; everything below is the
contract the daemons build on.

- [`hypeman_social.config`](#hypeman_socialconfig)
- [`hypeman_social.social`](#hypeman_socialsocial)
- [`hypeman_social.llm`](#hypeman_socialllm)
- [`hypeman_social.llm.guardrails`](#hypeman_socialllmguardrails)
- [`hypeman_social.observability`](#hypeman_socialobservability)

## `hypeman_social.config`

Configuration and secrets. Lookup order and every key: [CONFIGURATION.md](CONFIGURATION.md).

```python
from hypeman_social.config import (
    load_config, get_config, get_bool_config, get_int_config,
    get_float_config, get_usernames, get_secret,
)
```

| Function | Signature | Notes |
|---|---|---|
| `load_config` | `(env_path=".env") -> bool` | Load a `.env` file once; later calls no-op. Returns whether a file was found |
| `get_config` | `(section, key, default=None) -> str \| None` | Doppler → simple env → sectioned env → default. Empty string = unset |
| `get_bool_config` | `(section, key, default=False) -> bool` | Accepts true/1/yes/on/enabled |
| `get_int_config` | `(section, key, default=0) -> int` | Unparseable → default, with a warning |
| `get_float_config` | `(section, key, default=0.0) -> float` | Same fallback behavior |
| `get_usernames` | `(section, default=None) -> list[str]` | `TWITCH_USERNAMES=a,b` or singular `TWITCH_USERNAME=a`; plural wins |
| `get_secret` | `(platform, key, default=None, secret_name_env=None, secret_path_env=None, doppler_secret_env=None) -> str \| None` | Doppler → AWS → Vault → `<PLATFORM>_<KEY>` env → default |

Lower-level loaders (`load_secrets_from_aws(secret_name)`,
`load_secrets_from_vault(secret_path)`, `load_secrets_from_doppler(secret_name)`)
each return a dict and never raise — a secrets manager being down degrades to
env lookups, it doesn't crash the daemon.

## `hypeman_social.social`

```python
from hypeman_social.social import (
    SocialPlatform, REGISTRY,
    BlueskyPlatform, MastodonPlatform, DiscordPlatform, MatrixPlatform,
    EVENT_UPLOAD, EVENT_LIVE, EVENT_END, EVENT_STAR,
    event_kind, is_url_for_domain, platform_secret,
)
```

The module imports cleanly with **zero extras installed**; a platform whose
SDK is missing fails closed at `authenticate()` with a
`pip install 'hypeman-social[...]'` hint. That contract is tested in CI.

### `SocialPlatform` (abstract base)

| Member | Signature | Notes |
|---|---|---|
| `__init__` | `(name, enabled=False, **credentials)` | Keyword credentials override config lookup — for tests and daemons that wire creds themselves. `None` values are ignored |
| `authenticate` | `() -> bool` | Abstract. Establish credentials |
| `post` | `(message, reply_to_id=None, platform_name=None, stream_data=None) -> str \| None` | Abstract. Returns the new post's id (for threading) or None |
| `safe_post` | `(message, **kwargs) -> str \| None` | **Use this in daemons.** Never raises: one network having a bad day must not take down the daemon or block the other three |
| `is_ready` | `() -> bool` | enabled and authenticated |
| `test_connection` | `() -> bool` | Cheap liveness probe; platforms override where the API allows |
| `credential` | `(key, default=None) -> str \| None` | Constructor override, else `platform_secret` |
| `status` | `() -> dict` | `{name, enabled, authenticated, last_error}` for health endpoints |
| `char_limit` | class attr, `int \| None` | Platform character limit; None = no meaningful limit |

### `REGISTRY`

`{'bluesky': BlueskyPlatform, 'mastodon': ..., 'discord': ..., 'matrix': ...}` —
iterate it to construct every available network without naming them:

```python
platforms = [cls() for cls in REGISTRY.values()]
active = [p for p in platforms if p.authenticate()]
```

Adding a network means writing the module and adding one line here; every
daemon picks it up without changes.

### Event kinds

`stream_data['event_kind']` tells a platform what it's announcing, which
decides embed style, colors, and which metadata fields make sense:

| Constant | Value | Used by |
|---|---|---|
| `EVENT_UPLOAD` | `'upload'` | Boon-Tube-Daemon — "🎬 New YouTube Video" embeds, no viewer counts |
| `EVENT_LIVE` | `'live'` | stream-daemon — "🔴 Live" embeds with viewer count and category, edited in place on Discord |
| `EVENT_END` | `'end'` | stream ended — muted embed keeping the VOD link |
| `EVENT_STAR` | `'star'` | Star-Daemon — repository announcements |

Pass it explicitly. It used to be inferred from the platform name, and
"youtube" is ambiguous between an upload and a live broadcast — the inference
gave one daemon the wrong embed. `DiscordPlatform(default_event_kind=...)`
sets what to assume when a payload doesn't say; `event_kind(stream_data,
default)` reads it back out.

### `stream_data` payload

Free-form dict; recognized keys:

| Key | Used for |
|---|---|
| `title` | Embed titles/descriptions |
| `url` | Not needed — the first URL in the message text is used |
| `thumbnail_url` | Embed image (Discord), media upload (Mastodon), link-card thumb (Bluesky) |
| `viewer_count` | Live embeds; also marks content as live for Bluesky cards |
| `game_name` | Category field |
| `is_live` | Live vs video for Bluesky card copy |
| `description` | Bluesky card description for videos |
| `event_kind` | See above |

### Helpers

- `is_url_for_domain(url, domain) -> bool` — hostname-parsed domain check;
  `'youtube.com' in url` happily matches `evil-youtube.com.attacker.net`,
  this doesn't.
- `platform_secret(platform, key, default=None)` — `get_secret` with the
  conventional `SECRETS_*_<PLATFORM>_*` env names filled in.

## `hypeman_social.llm`

```python
from hypeman_social.llm import (
    LLMManager, BaseLLM, build_provider,
    ContentProfile, VIDEO_PROFILE, STREAM_PROFILE, STAR_PROFILE, GENERIC_PROFILE,
)
```

### `LLMManager`

One handle for "generate me a post", with automatic failover. This is what
daemons should hold — which provider actually served a request is an
implementation detail, visible in `status()`, not at the call site.

| Member | Signature | Notes |
|---|---|---|
| `__init__` | `(profile=GENERIC_PROFILE)` | Pick the [profile](#profiles) matching what you announce |
| `authenticate` | `() -> bool` | False only when `LLM_ENABLE` is off or no provider constructs. A primary that fails to *connect* is *not* fatal — it keeps retrying in the background |
| `is_available` | `() -> bool` | May heal a downed provider — the poll loop asking "can I use AI?" is what reconnects it |
| `generate` | `(prompt, max_tokens=None) -> str \| None` | Primary first, then the fallback chain. None = use your template |
| `apply_guardrails` | `(message, title, username, platform, char_limit, expected_hashtag_count=0) -> (str \| None, list[str])` | Full quality gauntlet; `(None, issues)` means don't post it |
| `heartbeat` | `(min_interval=None) -> bool` | Rate-limited liveness probe across every provider; call once per poll cycle so recovery is noticed on your schedule |
| `is_duplicate_message` / `add_to_message_cache` | | Dedup lives here, not on a provider, so failover doesn't wipe the recently-posted history |
| `status` | `() -> dict` | `{enabled, available, using_fallback, primary: {...}, fallbacks: [...]}` |
| `provider` | property | Primary provider name, or None |
| `active` | property | The provider that would serve the next request |

### `BaseLLM`

Shared provider behavior. Subclass only to add a provider; daemons should
use the manager. Public surface: `authenticate()`, `generate(prompt,
max_retries=None, max_tokens=None)`, `is_available()`, `heartbeat()`,
`mark_unavailable(error=None)`, `apply_guardrails(...)`, `status()`, plus the
config attributes listed in [CONFIGURATION.md](CONFIGURATION.md#llm-core).

**The availability contract: never branch on `.enabled` — always call
`is_available()`.** The reasoning is in [DESIGN.md](DESIGN.md#the-availability-contract).

Error handling inside `generate()`: connection errors mark the provider
unavailable and attempt one immediate reconnect; transient errors (429, 503,
quota, timeout) retry with exponential backoff; permanent errors (invalid,
unauthorized, not found) fail immediately.

### `build_provider(name, profile) -> BaseLLM | None`

Construct a single named provider (`'ollama'` or `'gemini'`) without the
manager — for callers that deliberately pin one backend. Returns None for
unknown/empty names.

### Profiles

`ContentProfile` is the domain vocabulary that differs between announcement
types: the noun for the thing ("video", "stream", "repository"), the filler
phrases that cost quality score, and the **hallucination patterns** — regexes
for details a model tends to invent, any one of which rejects the message.

| Profile | For | Rejects (among others) |
|---|---|---|
| `VIDEO_PROFILE` | video uploads (Boon-Tube-Daemon) | "premiering at 8", "live now", view counts |
| `STREAM_PROFILE` | live streams (stream-daemon) | "drops enabled", "VOD soon", viewer counts, raid claims |
| `STAR_PROFILE` | starred repos (Star-Daemon) | star/fork counts, version numbers, "trending" |
| `GENERIC_PROFILE` | everything else | giveaways, times, "special guest" |

Announcing something new? Build your own:

```python
from hypeman_social.llm import ContentProfile, LLMManager

PODCAST_PROFILE = ContentProfile(
    name='podcast',
    content_noun='episode',
    generic_phrases=['tune in', 'new episode', 'link below'],
    hallucination_patterns=[r'episode\s+\d+', r'\d+\s+downloads', r'featuring'],
)
manager = LLMManager(profile=PODCAST_PROFILE)
```

## `hypeman_social.llm.guardrails`

The stateless checks behind `apply_guardrails`, importable individually for
custom validation flows (stream-daemon's lenient retry loop uses them
directly):

| Function | Purpose |
|---|---|
| `validate_message_quality(message, expected_hashtag_count, title, username, profile)` | Hard pass/fail: hashtag count, forbidden words, inline URLs, profile hallucinations. `(bool, issues)` |
| `score_message_quality(message, title, profile)` | Soft 0–10 score with reasons |
| `validate_platform_specific(message, platform)` | @everyone on Discord, inline URLs on Bluesky, HTML entities on Mastodon, malformed handles |
| `contains_forbidden_words(message)` | The cringe list: INSANE, EPIC, "smash that", … |
| `contains_profanity(message, severity='moderate')` | Word lists by severity |
| `validate_hashtags_against_username(message, username)` | Strips `#YourOwnName` tags — returns the cleaned message |
| `extract_hashtags(message)` / `remove_hashtag_from_message(message, tag)` | Hashtag utilities |
| `count_emojis(message)` | Real emoji counting, not len() tricks |
| `safe_trim(message, limit)` | Trim to a limit without cutting a hashtag mid-word |
| `extract_from_thinking(thinking, max_chars=300)` | Mine a usable post out of a reasoning model's thinking text |
| `tokenize_username(username)` | Username → token set, for self-reference checks |

## `hypeman_social.observability`

```python
from hypeman_social.observability import HealthState, start_health_server, configure_logging
```

### `HealthState(service_name)`

Thread-safe snapshot of what a daemon is doing:

- `register(name, provider)` — live status source, called on every read
  (register `manager.status` here so the LLM's state is always current)
- `set_component(name, healthy, detail=None)` — direct health flag
- `record_event(name, detail=None)` — timestamped "last_poll", "last_post", …
- `snapshot() -> dict` — full state
- `is_healthy() -> bool` — only components explicitly marked unhealthy count.
  A degraded LLM does **not** fail health: a daemon posting template messages
  because the AI box is offline is degraded, not down, and a health check
  that cries wolf gets ignored.

### `start_health_server(state, port, host='127.0.0.1') -> HTTPServer | None`

Serves on a background daemon thread:

- `GET /healthz` — 200 alive / 503 something essential broke
- `GET /status` — always 200, full JSON detail

`port=0` disables it; a busy port logs an error and returns None rather than
stopping the daemon. Binds to localhost by default — the status output is a
free recon gift, so expose `0.0.0.0` only behind access control.

### `configure_logging(level=None, log_file=None, force=False)`

One-call logging setup: rotating file output, systemd detection (timestamps
off under journald), repeat-message deduplication, quieting chatty HTTP
libraries. Keys in [CONFIGURATION.md](CONFIGURATION.md#logging).
