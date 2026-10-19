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
- [LLM: Claude](#llm-claude)
- [LLM: OpenAI](#llm-openai)
- [LLM: guardrails](#llm-guardrails)
- [LLM: recovery](#llm-recovery)
- [Social: Bluesky](#social-bluesky)
- [Social: Mastodon](#social-mastodon)
- [Social: Discord](#social-discord)
- [Chat: Discord (receiving)](#chat-discord-receiving)
- [Social: Matrix](#social-matrix)
- [Chat: Twitch](#chat-twitch)
- [Logging](#logging)
- [Worked examples](#worked-examples)

## How lookup works

### Plain configuration — `get_config(section, key)`

Priority, first match wins:

1. **Doppler** — if `DOPPLER_TOKEN` is set, the sectioned key
   (`SETTINGS_CHECK_INTERVAL`) then the simple key (`CHECK_INTERVAL`) are
   looked up in the Doppler project/config. The project is fetched once per
   process and shared with the secret lookups below; `reset_secret_cache()`
   forces a re-read.
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
| `DOPPLER_CONFIG` | Doppler config | `prd` |
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
capability never see the parameter. With thinking mode off (the default) a
thinking-capable model is sent `think=false`, so a one-sentence chat answer is
not spent on hidden reasoning.

## LLM: provider selection and failover

| Variable | Meaning | Default |
|---|---|---|
| `LLM_PROVIDER` | Primary provider: `ollama`, `gemini`, `claude` (alias `anthropic`) or `openai` | `gemini` |
| `LLM_FALLBACK_PROVIDER` | Ordered, comma-separated failover chain, e.g. `gemini` or `gemini,ollama`. **Strictly opt-in**: if you chose Ollama so your data stays local, hypeman will never silently ship prompts to Google | *(none)* |
| `LLM_MODEL` | Model name shared by all providers | provider default |
| `LLM_OLLAMA_MODEL` / `LLM_GEMINI_MODEL` / `LLM_CLAUDE_MODEL` / `LLM_OPENAI_MODEL` | Per-provider model; wins over `LLM_MODEL`. Needed whenever primary and fallback run different model names | provider default |

**Claude and OpenAI read only their own namespace.** Every setting of those two
providers (`LLM_CLAUDE_*`, `LLM_OPENAI_*`, plus the shared `LLM_MODEL`) is looked
up by that exact name, Doppler first and then the environment. Unlike the
other providers, the bare names (`OPENAI_BASE_URL`, `OPENAI_MODEL`,
`CLAUDE_MODEL`, `CLAUDE_EFFORT`, ...) are ignored, so a variable set for other
tooling cannot redirect the endpoint that receives your API key. `claude` and
`anthropic` name the same provider, so listing both in
`LLM_FALLBACK_PROVIDER` builds one client. A malformed number
(`LLM_*_TIMEOUT`, `LLM_*_RATE_LIMIT`) logs a warning naming the setting and
uses the default; it never stops startup. That includes the shared numeric
settings these two providers inherit (`LLM_MAX_TOKENS`, `LLM_MAX_RETRIES`,
`LLM_TEMPERATURE`, ...); the other providers still fail fast on a typo.

**Where the key goes.** What is guaranteed, and what is not:

- Both clients are built with an explicit base URL (`https://api.anthropic.com`,
  `https://api.openai.com/v1`, or your validated `LLM_OPENAI_BASE_URL`), so
  `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` in the environment are never used.
- Redirects are disabled: a 3xx is an error, never followed with the key.
- The HTTP client ignores the process environment for network trust
  (`trust_env=False`): `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`,
  `SSL_CERT_FILE`, `SSL_CERT_DIR` and `REQUESTS_CA_BUNDLE` have no effect. Anyone
  who controls those could otherwise terminate TLS for the pinned host with
  their own CA and read the key. For a private proxy or CA use the settings
  below; both are read by exact name (Doppler, then env) and an invalid value
  makes that provider unavailable with one error naming the setting, never a
  silent fallback to the environment.

  | Variable | Meaning |
  |---|---|
  | `LLM_CLAUDE_PROXY` / `LLM_OPENAI_PROXY` | `http://` or `https://` proxy URL with a host. Credentials in the URL are refused (they would land in config and logs), as are query and fragment |
  | `LLM_CLAUDE_CA_BUNDLE` / `LLM_OPENAI_CA_BUNDLE` | Path to a readable PEM file. It **replaces** the default trust store for that provider's client |

- Not covered: someone who can set `LLM_*` settings, edit the config source
  (Doppler project, `.env`), or run code in the process controls the key
  anyway. The headers `OPENAI_ORG_ID` / `OPENAI_PROJECT_ID` and
  `OPENAI_CUSTOM_HEADERS` / `ANTHROPIC_CUSTOM_HEADERS` are still read by the
  SDKs and sent to the pinned host only (custom headers can replace an auth
  header, so do not set them in this process's environment unless you mean to).
  `ANTHROPIC_AUTH_TOKEN` and Anthropic profile files are not consulted because
  the API key is passed explicitly.
- Error handling: provider errors carry only a fixed safe reason (class,
  status, "connection error", ...), never SDK text, and are raised after the
  SDK handler exits so they have no chained exception on the provider's own
  path. If *your* code calls the provider from inside an `except` block,
  Python attaches that outer exception; the provider marks it suppressed so
  formatted tracebacks do not print it, but the object stays reachable. Object
  level secrecy is not claimed. The API key also stays on the provider
  instance (reconnect needs it), so a traceback's frame locals or a debugger
  can reach it; do not ship full-locals tracebacks (for example
  `sentry` with `include_local_variables`) from this process.
- Structured-output enums are limited to 256 plain-JSON options, 8 levels, 2000
  nodes and 16,384 characters of strings and object keys in total; a larger enum
  is refused before the call.

The manager keeps re-probing a downed primary in the background and switches
back automatically when it recovers — you stop paying for the cloud fallback
the moment your local box is back.

## LLM: Ollama

Install extra: `hypeman-social[ollama]`.

| Variable | Meaning | Default |
|---|---|---|
| `LLM_OLLAMA_HOST` | Server host; `http://` is assumed when no scheme given | `http://localhost` |
| `LLM_OLLAMA_PORT` | Port, appended only when the host has none | `11434` |
| `LLM_OLLAMA_MODEL` | Model, e.g. `gemma4:12b`, `gemma3:4b`, `qwen3:8b` | `gemma4:12b` |
| `LLM_OLLAMA_NUM_CTX` | Context size, sent as `options.num_ctx` exactly as written. Leave unset to send nothing. On a shared GPU, a `num_ctx` different from the one the model is loaded with makes Ollama reload it and can evict another resident model, so set this only to the value your server already uses | *(not sent)* |
| `LLM_OLLAMA_KEEP_ALIVE` | Passed as `keep_alive` when set (e.g. `30s`, `5m`), so a non-resident model can be given a short residency | *(not sent)* |

A model missing from the server logs a warning but doesn't fail —
Ollama pulls it on first use.

## LLM: Gemini

Install extra: `hypeman-social[gemini]`.

| Variable | Meaning | Default |
|---|---|---|
| `LLM_GEMINI_API_KEY` | API key (secret chain applies; `GEMINI_API_KEY` also accepted) | — |
| `LLM_GEMINI_MODEL` | Model name | `gemini-2.0-flash-lite` |
| `LLM_GEMINI_RATE_LIMIT` | Client-side requests/minute self-throttle, so you never discover the free-tier limit via 429s | `30` |

## LLM: Claude

Install extra: `hypeman-social[claude]` (the Anthropic SDK needs Python 3.10 or newer; on 3.9 the extra installs nothing and the provider reports itself unavailable). Set `LLM_PROVIDER=claude` (or put
`claude` in `LLM_FALLBACK_PROVIDER`). Built for high-volume chat, so the
defaults are the cheap path.

| Variable | Meaning | Default |
|---|---|---|
| `LLM_CLAUDE_API_KEY` | API key (secret chain applies, Doppler first; `ANTHROPIC_API_KEY` also accepted) | — |
| `LLM_CLAUDE_MODEL` | `claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5` or `claude-fable-5-1` | `claude-haiku-5-5` |
| `LLM_CLAUDE_SYSTEM_PROMPT` | Stable persona/rules prefix sent as the system block. Keep it byte-identical between calls | *(none)* |
| `LLM_CLAUDE_PROMPT_CACHE` | Put a `cache_control` breakpoint on the system block | `true` |
| `LLM_CLAUDE_CACHE_TTL` | `5m` or `1h` | `5m` |
| `LLM_CLAUDE_TIMEOUT` | Per-request timeout, seconds | `30` |
| `LLM_CLAUDE_EFFORT_LEVEL` | `low`, `medium`, `high`, `xhigh`, `max`, or `omit` for the model default | `low` |
| `LLM_CLAUDE_THINKING` | `auto`, `disabled`, `adaptive`, `between_tools`, `omit`. `auto` sends `disabled` on Haiku and omits it elsewhere (Sonnet and Opus 5.5 reject `disabled`) | `auto` |
| `LLM_CLAUDE_RATE_LIMIT` | Client-side requests/minute self-throttle; `0` disables | `50` |

What the options cost:

- **Prompt caching** is the main saving when `LLM_CLAUDE_SYSTEM_PROMPT` is
  long and calls are frequent: cache reads bill at about 0.1x the input rate,
  the first write at 1.25x (5 minute TTL) or 2x (1 hour TTL). A prefix below
  the model's minimum cacheable length silently does not cache, so a short
  persona gains nothing; check `usage.cache_read_input_tokens`. Use `1h` only
  when calls are minutes apart.
- **Effort and thinking** spend output tokens. `low` effort with thinking off
  keeps a chat reply near its visible length. Raising either costs output
  tokens, billed at the output rate, and thinking shares the `max_tokens` cap.
- **Sampling parameters** (`temperature`, `top_p`) are never sent, because
  Haiku 5.5 and Sonnet 5.5 reject non-default values. `LLM_TEMPERATURE` and
  `LLM_TOP_P` therefore do not apply to Claude; steer from the prompt.
- Limits and retries: the SDK's own retries are off, so `LLM_MAX_RETRIES` and
  `LLM_RETRY_DELAY_BASE` are the only retry layer. A safety decline
  (`stop_reason: refusal`) returns no text, which the manager treats like any
  other failed generation (next provider, or the template).

Haiku 5.5 is $0.10 / $0.50 per million input / output tokens up to 100K
prompt tokens; check the Anthropic pricing page before committing a budget.

## LLM: OpenAI

Install extra: `hypeman-social[openai]` (the OpenAI SDK needs Python 3.10 or newer; on 3.9 the extra installs nothing and the provider reports itself unavailable). Uses the Responses API.

| Variable | Meaning | Default |
|---|---|---|
| `LLM_OPENAI_API_KEY` | API key (secret chain applies, Doppler first; `OPENAI_API_KEY` also accepted) | — |
| `LLM_OPENAI_MODEL` | Model id | `gpt-6-luna` |
| `LLM_OPENAI_SYSTEM_PROMPT` | Sent as `instructions` | *(none)* |
| `LLM_OPENAI_REASONING_EFFORT` | `none`, `low`, `medium`, `high`, `xhigh`, `max`, or `omit` for the model default | `low` |
| `LLM_OPENAI_TIMEOUT` | Per-request timeout, seconds | `30` |
| `LLM_OPENAI_BASE_URL` | Only for an OpenAI-compatible gateway; unset uses OpenAI. Must be `https` (plain `http` only for `localhost`/loopback) with no user info, query or fragment; anything else fails authentication and the key is never attached to it | *(none)* |
| `LLM_OPENAI_RATE_LIMIT` | Client-side requests/minute self-throttle; `0` disables | `60` |

Requests are sent with `store: false`, and `temperature`/`top_p` are not sent
(current models do not document them). Reasoning tokens are billed as output
tokens, so `low` keeps chat cheap; raise it only for work that needs it.
`gpt-6-luna` is listed at $0.10 / $0.50 per million input / output tokens;
check the OpenAI pricing page before committing a budget.

**Structured output.** `OpenAILLM.generate_structured(prompt, schema, name)`
sends a strict JSON schema and returns the parsed object, or `None`. `None`
covers a model refusal, an incomplete (truncated) response, text that is not
JSON (duplicate keys, `NaN` and `Infinity` count as invalid), and any
mismatch with your schema, checked locally: `type`, `properties`, `required`,
`additionalProperties`, `enum` (at most 256 plain-JSON options, 8 levels deep)
and `items`; `integer` accepts `1.0` and `1e0` as JSON Schema does. A schema using any other keyword
(`pattern`, `minimum`, `oneOf`, ...) is refused before the call, because the
result could not be verified. It never repairs or guesses,
so a safety classifier should treat `None` as "no verdict" and fail closed.
It shares availability, throttling, retry and error handling with
`generate()`. Strict mode requires `additionalProperties: false` and every
property listed in `required`. Schemas add a small first-call latency.

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
under the root post. `stream_data['images']` attaches up to four pictures
with alt text as an images embed (which replaces the link card — Bluesky
allows one embed per post).

## Social: Mastodon

Install extra: `hypeman-social[mastodon]`.

| Variable | Meaning | Default |
|---|---|---|
| `MASTODON_ENABLE_POSTING` | Enable the platform | `false` |
| `MASTODON_API_BASE_URL` | Your instance, e.g. `https://infosec.exchange` | — |
| `MASTODON_ACCESS_TOKEN` | Access token (secret chain) — Preferences → Development → New Application, scopes `read`, `write:statuses`, `write:media` | — |
| `MASTODON_CLIENT_ID` | OAuth client id (secret chain). Optional; only useful together with the secret | — |
| `MASTODON_CLIENT_SECRET` | OAuth client secret (secret chain). Optional | — |

Behavior: replies thread; thumbnails and `images` upload as media
attachments with alt text; a failed upload never blocks the post. The
access token alone is enough to post — the client id/secret pair is passed
through when both are set and ignored (with a warning) when only one is.

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

## Chat: Discord (receiving)

Needs the `discord-chat` extra (`websocket-client`). Setup, the privileged
intent and the bridge notes are in [DISCORD_CHAT.md](DISCORD_CHAT.md). Every
key goes through the usual secret chain (Doppler first, then the environment),
as `DISCORD_<KEY>`.

| Variable | Meaning | Default |
|---|---|---|
| `DISCORD_BOT_TOKEN` | Bot token (secret chain). Never logged | required |
| `DISCORD_GUILD_ID` | Only hear this guild | all guilds the bot is in |
| `DISCORD_CHANNEL_IDS` | Comma-separated channel ids to hear | all text channels the bot can see |
| `DISCORD_MAX_EVENT_BYTES` | Largest inbound Gateway frame or message, in bytes. An event over it stops the receiver with one error naming this setting. Positive integer, at most 67108864; anything else fails closed | `2097152` (2 MiB) |
| `DISCORD_MODERATOR_ROLE_IDS` | Comma-separated role ids that map to the moderator role | none (nobody is a moderator) |

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

## Social: Threads

No extra needed — the Threads Graph API is plain HTTPS.

| Variable | Meaning | Default |
|---|---|---|
| `THREADS_ENABLE_POSTING` | Enable the platform | `false` |
| `THREADS_ACCESS_TOKEN` | Long-lived user access token | — |
| `THREADS_USER_ID` | Numeric Threads user ID | — |

Behavior: two-step publish (create a media container, then publish it);
500-character limit enforced with URL-preserving trims; the first URL in the
message is attached as an explicit link-preview card; replies thread via
`reply_to_id`.

Getting credentials: create a Meta app at
[developers.facebook.com](https://developers.facebook.com/), add the
**Threads API** use case, grant `threads_basic` and
`threads_content_publish`, then generate a long-lived access token for your
account. Your numeric user ID comes back from
`GET https://graph.threads.net/v1.0/me?fields=id`. Long-lived tokens last
~60 days; refresh them with `GET .../refresh_access_token` before expiry.

## Chat: Twitch

Needs the `twitch` extra (`pip install "hypeman-social[twitch]"`, which adds
`websocket-client`). Hears one channel and replies as a dedicated bot account.
Setup, scopes and revocation are in [TWITCH_BOT.md](TWITCH_BOT.md).

Every key is read through the secret chain (Doppler first) as
`platform_secret('Twitch', key)`; there are no plain settings.

| Key | Meaning | Required |
|---|---|---|
| `client_id` | Client ID of the bot's own Twitch application | yes |
| `client_secret` | That application's client secret; used only to refresh the token | for refresh |
| `access_token` | The bot account's user access token (`user:read:chat`, `user:write:chat`) | yes |
| `refresh_token` | The matching refresh token | for refresh |
| `bot_user_id` | Numeric Twitch user id of the bot account | yes |
| `channel` | Channel to join, as a login name or a numeric user id | yes |

The adapter shares the `TWITCH_` prefix with any other Twitch credentials in
the same Doppler config (for example a stream announcer's own application). Point
the bot at its own bundle with `SECRETS_DOPPLER_TWITCH_SECRET_NAME` (for example
`TWITCHBOT`, so keys read `TWITCHBOT_CLIENT_ID`), or give it its own Doppler config.

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
LLM_OLLAMA_MODEL=gemma4:12b
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
LLM_OLLAMA_MODEL=gemma4:12b
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
