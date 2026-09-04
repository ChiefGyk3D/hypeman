# Configuration Reference

Every setting hypeman-social reads, with defaults. Nothing here is required to
*import* the library — configuration is read lazily, when a component
authenticates or generates.

- [How lookup works](#how-lookup-works)
- [Secrets managers](#secrets-managers)
- [LLM: core](#llm-core)
- [LLM: provider selection and failover](#llm-provider-selection-and-failover)
- [LLM: Ollama](#llm-ollama)
- [LLM: Gemini](#llm-gemini)
- [LLM: guardrails](#llm-guardrails)
- [LLM: recovery](#llm-recovery)
- [Social: Bluesky](#social-bluesky)
- [Social: Mastodon](#social-mastodon)
- [Social: Discord](#social-discord)
- [Social: Matrix](#social-matrix)
- [Logging](#logging)
- [Worked examples](#worked-examples)

## How lookup works

### Plain configuration — `get_config(section, key)`

Priority, first match wins:

1. **Doppler** — if `DOPPLER_TOKEN` is set, the sectioned key
   (`SETTINGS_CHECK_INTERVAL`) then the simple key (`CHECK_INTERVAL`) are
   looked up in the Doppler project/config.
2. **Simple env key** — `CHECK_INTERVAL`
3. **Sectioned env key** — `SETTINGS_CHECK_INTERVAL`
4. The default passed by the caller.

Empty strings count as unset — a blank line in `.env` never overrides a
default. Values beginning with `YOUR_` are treated as unedited template
placeholders and skipped.

Booleans accept `true`, `1`, `yes`, `on`, `enabled` (case-insensitive);
anything else is false. Unparseable ints/floats fall back to the default with
a logged warning.

This document lists keys in their **sectioned** form (`LLM_PROVIDER`,
`BLUESKY_HANDLE`, …), which is the convention all four daemons use. The simple
form (`PROVIDER`) also resolves, but don't — it collides across sections.

### Secrets — `get_secret(platform, key)`

Credentials go through a stricter chain, so a production secrets manager
genuinely overrides a stale local `.env`:

1. **Doppler** (if `DOPPLER_TOKEN` is set)
2. **AWS Secrets Manager** (if `SECRETS_MANAGER=aws`)
3. **HashiCorp Vault** (if `SECRETS_MANAGER=vault`)
4. **Environment / `.env`** — `<PLATFORM>_<KEY>`, e.g. `BLUESKY_APP_PASSWORD`
5. The default.

`load_config(env_path=".env")` loads a `.env` file into the environment;
call it once at startup (repeat calls are cheap no-ops).

## Secrets managers

All optional. With none configured, everything reads from env / `.env`.

| Variable | Meaning | Default |
|---|---|---|
| `DOPPLER_TOKEN` | Doppler service token; presence enables Doppler | — |
| `DOPPLER_PROJECT` | Doppler project | — |
| `DOPPLER_CONFIG` | Doppler config | `dev` (config lookups) / `prd` (secret bundles) |
| `SECRETS_MANAGER` | `aws`, `vault`, or `none` | `none` |
| `SECRETS_VAULT_URL` | Vault server URL | — |
| `SECRETS_VAULT_TOKEN` | Vault token | — |

Per-platform bundle locations (all optional; a conventional `<platform>`
bundle name is tried when unset):

| Variable pattern | Example |
|---|---|
| `SECRETS_AWS_<PLATFORM>_SECRET_NAME` | `SECRETS_AWS_BLUESKY_SECRET_NAME=prod/bluesky` |
| `SECRETS_VAULT_<PLATFORM>_SECRET_PATH` | `SECRETS_VAULT_BLUESKY_SECRET_PATH=secret/bluesky` |
| `SECRETS_DOPPLER_<PLATFORM>_SECRET_NAME` | `SECRETS_DOPPLER_BLUESKY_SECRET_NAME=BLUESKY` |

AWS credentials/region come from the standard boto3 chain
(`AWS_ACCESS_KEY_ID`, `AWS_DEFAULT_REGION`, instance profiles, …).

Install the matching extra: `hypeman-social[aws]`, `[vault]`, `[doppler]`.

## LLM: core

| Variable | Meaning | Default |
|---|---|---|
| `LLM_ENABLE` | Master switch. Off = `LLMManager.authenticate()` returns False and callers use their template fallbacks | `false` |
| `LLM_MAX_TOKENS` | Response token budget per generation | `150` |
| `LLM_TEMPERATURE` | Sampling temperature. Low by default because small models get "creative" and invent giveaways | `0.3` |
| `LLM_TOP_P` | Nucleus sampling | `0.9` |
| `LLM_MAX_RETRIES` | Attempts per generation before giving up | `3` |
| `LLM_RETRY_DELAY_BASE` | Seconds before first retry; doubles each attempt | `2` |

### Thinking mode (reasoning models: qwen3, gemma4, deepseek-r1)

| Variable | Meaning | Default |
|---|---|---|
| `LLM_ENABLE_THINKING_MODE` | Give reasoning models headroom to think, and mine the thinking text when the answer field comes back empty | `false` |
| `LLM_THINKING_TOKEN_MULTIPLIER` | Token budget multiplier while thinking mode is on | `4.0` |

When the connected Ollama model advertises the `thinking` capability, hypeman
passes an explicit `think` parameter on every generation: `think=false` forces
a direct answer, `think=true` routes reasoning into the response's thinking
field where extraction can reach it. Models (and old servers) without the
capability never see the parameter.

## LLM: provider selection and failover

| Variable | Meaning | Default |
|---|---|---|
| `LLM_PROVIDER` | Primary provider: `ollama` or `gemini` | `gemini` |
| `LLM_FALLBACK_PROVIDER` | Ordered, comma-separated failover chain, e.g. `gemini` or `gemini,ollama`. **Strictly opt-in**: if you chose Ollama so your data stays local, hypeman will never silently ship prompts to Google | *(none)* |
| `LLM_MODEL` | Model name shared by all providers | provider default |
| `LLM_OLLAMA_MODEL` / `LLM_GEMINI_MODEL` | Per-provider model; wins over `LLM_MODEL`. Needed whenever primary and fallback run different model names | provider default |

The manager keeps re-probing a downed primary in the background and switches
back automatically when it recovers — you stop paying for the cloud fallback
the moment your local box is back.

## LLM: Ollama

Install extra: `hypeman-social[ollama]`.

| Variable | Meaning | Default |
|---|---|---|
| `LLM_OLLAMA_HOST` | Server host; `http://` is assumed when no scheme given | `http://localhost` |
| `LLM_OLLAMA_PORT` | Port, appended only when the host has none | `11434` |
| `LLM_OLLAMA_MODEL` | Model, e.g. `gemma3:4b`, `qwen3:8b`, `llama3` | `gemma3:4b` |

A model missing from the server logs a warning but doesn't fail —
Ollama pulls it on first use.

## LLM: Gemini

Install extra: `hypeman-social[gemini]`.

| Variable | Meaning | Default |
|---|---|---|
| `LLM_GEMINI_API_KEY` | API key (secret chain applies; `GEMINI_API_KEY` also accepted) | — |
| `LLM_GEMINI_MODEL` | Model name | `gemini-2.0-flash-lite` |
| `LLM_GEMINI_RATE_LIMIT` | Client-side requests/minute self-throttle, so you never discover the free-tier limit via 429s | `30` |

## LLM: guardrails

Applied by `apply_guardrails()` after generation; a failed hard check returns
`None` and the daemon posts its template instead.

| Variable | Meaning | Default |
|---|---|---|
| `LLM_ENABLE_DEDUPLICATION` | Reject messages too similar to recent ones (>80% word overlap). Lives on the manager, so failover doesn't wipe the history | `true` |
| `LLM_DEDUP_CACHE_SIZE` | Recent messages remembered | `20` |
| `LLM_MAX_EMOJI_COUNT` | More than this reads as spam | `2` |
| `LLM_ENABLE_PROFANITY_FILTER` | Reject profanity | `false` |
| `LLM_PROFANITY_SEVERITY` | `mild`, `moderate`, or `severe` — which word lists to check. `moderate` checks mild+moderate words; `severe` checks everything | `moderate` |
| `LLM_ENABLE_QUALITY_SCORING` | Score 0–10 against generic-phrase and relevance heuristics | `false` |
| `LLM_MIN_QUALITY_SCORE` | Minimum passing score | `6` |
| `LLM_ENABLE_PLATFORM_VALIDATION` | Per-network checks (@everyone on Discord, inline URLs on Bluesky, HTML entities on Mastodon) | `true` |

Not configurable — always on: hallucination detection from the active
[content profile](API.md#profiles) (invented star counts, "premiering
tonight", "drops enabled", …), forbidden hype-word list, exact hashtag-count
enforcement, and stripping hashtags derived from your own username.

## LLM: recovery

The part that exists because of [the outage](DESIGN.md#the-availability-contract).

| Variable | Meaning | Default |
|---|---|---|
| `LLM_ENABLE_AUTO_RECONNECT` | Heal a downed provider on the next availability check | `true` |
| `LLM_RECONNECT_INTERVAL` | Cooldown between reconnect attempts (seconds) | `60` |
| `LLM_MAX_RECONNECT_ATTEMPTS` | Give up after N attempts; `0` = never give up | `0` |

## Social: Bluesky

Install extra: `hypeman-social[bluesky]`.

| Variable | Meaning | Default |
|---|---|---|
| `BLUESKY_ENABLE_POSTING` | Enable the platform | `false` |
| `BLUESKY_HANDLE` | Account handle, e.g. `you.bsky.social` | — |
| `BLUESKY_APP_PASSWORD` | App password (secret chain applies) — create one at Settings → App Passwords, never your account password | — |

Behavior: 300-grapheme limit enforced with real grapheme counting (ZWJ emoji
count as one), URLs become link facets, `#hashtags` become tag facets, link
cards are built from stream metadata or an Open Graph scrape, replies thread
under the root post.

## Social: Mastodon

Install extra: `hypeman-social[mastodon]`.

| Variable | Meaning | Default |
|---|---|---|
| `MASTODON_ENABLE_POSTING` | Enable the platform | `false` |
| `MASTODON_API_BASE_URL` | Your instance, e.g. `https://infosec.exchange` | — |
| `MASTODON_CLIENT_ID` | OAuth client id (secret chain) | — |
| `MASTODON_CLIENT_SECRET` | OAuth client secret (secret chain) | — |
| `MASTODON_ACCESS_TOKEN` | Access token (secret chain) | — |

Behavior: replies thread, thumbnails upload as media attachments with alt
text; a failed thumbnail never blocks the post.

## Social: Discord

No extra needed — plain webhooks over HTTP.

| Variable | Meaning | Default |
|---|---|---|
| `DISCORD_ENABLE_POSTING` | Enable the platform | `false` |
| `DISCORD_WEBHOOK_URL` | Default webhook (secret chain) | — |
| `DISCORD_WEBHOOK_<PLATFORM>` | Per-source webhook: `DISCORD_WEBHOOK_TWITCH`, `_YOUTUBE`, `_KICK`, `_TIKTOK` route each source to its own channel | falls back to default |
| `DISCORD_ROLE` | Role id to mention on posts | — |
| `DISCORD_ROLE_<PLATFORM>` | Per-source role mention | falls back to default |
| `DISCORD_ENDED_MESSAGE` | Text shown when a live embed flips to "stream ended" | built-in |
| `DISCORD_ENDED_MESSAGE_<PLATFORM>` | Per-source ended text | falls back to default |

Behavior: rich embeds colored per source (Twitch purple, YouTube red, Kick
green, TikTok cyan), live embeds are **edited in place** with fresh viewer
counts and thumbnails, and flip to a muted "ended" embed keeping the VOD
link. The event kind (`upload` vs `live`) decides embed style — pass it
explicitly (see [API](API.md#event-kinds)).

## Social: Matrix

No extra needed — client-server HTTP API via requests.

| Variable | Meaning | Default |
|---|---|---|
| `MATRIX_ENABLE_POSTING` | Enable the platform | `false` |
| `MATRIX_HOMESERVER` | Homeserver URL; `https://` assumed | — |
| `MATRIX_ROOM_ID` | Target room, e.g. `!abc123:matrix.org` | — |
| `MATRIX_USERNAME` + `MATRIX_PASSWORD` | Bot login — **preferred**: a fresh access token per start, tokens rotate automatically | — |
| `MATRIX_ACCESS_TOKEN` | Static token — used only when username/password are absent | — |

Behavior: HTML-formatted messages with clickable links and per-source
headers; replies use `m.in_reply_to`. Matrix cannot edit messages, so no
live viewer-count updates.

## Logging

Optional; use `configure_logging()` from `hypeman_social.observability`.

| Variable | Meaning | Default |
|---|---|---|
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` | `INFO` |
| `LOG_FILE` | Path; enables rotating file output | *(stdout only)* |
| `LOG_MAX_BYTES` | Rotate at this size | `10485760` (10 MB) |
| `LOG_BACKUP_COUNT` | Rotated files kept | `5` |
| `LOG_TO_STDOUT` | Also log to stdout | `true` |
| `LOG_TIMESTAMPS` | Include timestamps (auto-off under systemd, where journald stamps already) | auto |
| `LOG_DEDUPE_SECONDS` | Collapse identical repeats within N seconds | `0` (off) |
| `LOG_QUIET_LIBRARIES` | Quiet chatty HTTP libraries | `true` |

## Worked examples

### Local-first AI with cloud failover, posting everywhere

```bash
LLM_ENABLE=true
LLM_PROVIDER=ollama
LLM_OLLAMA_HOST=http://192.168.1.50
LLM_OLLAMA_MODEL=gemma3:4b
LLM_FALLBACK_PROVIDER=gemini
LLM_GEMINI_MODEL=gemini-2.0-flash-lite
GEMINI_API_KEY=...

BLUESKY_ENABLE_POSTING=true
BLUESKY_HANDLE=you.bsky.social
BLUESKY_APP_PASSWORD=...

MASTODON_ENABLE_POSTING=true
MASTODON_API_BASE_URL=https://your.instance
MASTODON_CLIENT_ID=...
MASTODON_CLIENT_SECRET=...
MASTODON_ACCESS_TOKEN=...

DISCORD_ENABLE_POSTING=true
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

MATRIX_ENABLE_POSTING=true
MATRIX_HOMESERVER=https://matrix.org
MATRIX_ROOM_ID=!room:matrix.org
MATRIX_USERNAME=@bot:matrix.org
MATRIX_PASSWORD=...
```

### Fully local, nothing leaves the network

```bash
LLM_ENABLE=true
LLM_PROVIDER=ollama
LLM_OLLAMA_HOST=http://localhost
LLM_OLLAMA_MODEL=gemma3:4b
# no LLM_FALLBACK_PROVIDER: an Ollama outage means template posts, not Google
```

### Production: Doppler holds the secrets

```bash
DOPPLER_TOKEN=dp.st....
DOPPLER_PROJECT=stream-daemon
DOPPLER_CONFIG=prd
# every key above can live in Doppler instead of this file,
# and Doppler wins over anything set locally
```
