# Design Notes

Why hypeman-social is shaped the way it is. Most of these decisions were
paid for in production before they became rules.

## Origin: three copies, drifting

Boon-Tube-Daemon, stream-daemon, and Star-Daemon each carried their own
copies of the same social-posting and LLM code — roughly 55–65% identical
line-for-line, with the LLM layer sharing 352 identical lines across files
that had already diverged. A bug fixed in one daemon never reached the
others. This library exists so a fix lands once and protects everyone.

The library deliberately knows nothing about *what* is being announced.
Videos, streams, stars, jokes — that's the caller's business. What's shared
is the shouting: where to post, how to survive failure, and how to keep a
small model honest.

## The availability contract

**Never branch on `.enabled`. Always call `is_available()`.**

The outage that motivated the extraction: an auto-reconnect fix for a local
Ollama server was written for one daemon and never reached the other — and in
the daemon that *had* it, the fix was unreachable, because every call site
gated on an `enabled` flag that the failure path set to `False` and nothing
ever set back. Taking the AI box offline meant no AI until a human noticed
and restarted the process.

The lesson generalizes: **a gate that can only ever go from working to broken
is not a health check, it's a latch.** So in hypeman:

- `is_available()` is allowed to *heal*: if the provider is down but
  configuration is intact, it attempts a cooldown-guarded reconnect and
  reports the result. The daemon's own poll loop asking "can I use AI?" is
  the thing that fixes the connection.
- A failed `authenticate()` is "not right now", never "never". Config is
  retained so recovery has a target.
- `heartbeat()` runs the same probe on a timer, so recovery is noticed
  between announcements — the first post after an outage uses AI, not a
  template.
- Recovery is paced (`LLM_RECONNECT_INTERVAL`, default 60s) and by default
  never gives up (`LLM_MAX_RECONNECT_ATTEMPTS=0`).

## Degraded is not dead

An LLM outage must cost you *message quality*, not *messages*. Every layer
enforces this:

- `LLMManager.generate()` returning None is a normal, expected state; every
  daemon keeps a template path.
- `HealthState.is_healthy()` ignores provider-supplied status: a daemon
  posting template messages because the AI box is offline reports healthy.
  A health check that cries wolf gets ignored, and then it misses the real
  outage.
- `SocialPlatform.safe_post()` never raises: one network having a bad day
  doesn't take down the daemon or block the others.
- Optional SDKs fail closed at `authenticate()` with an install hint —
  never at import. A daemon that only posts to Discord must not need
  atproto on disk. (CI proves this with a bare-install job.)

## Failover is opt-in, and it self-heals backwards

Ollama and Gemini have complementary failure modes — the local box dies to
floods, power cuts, and GPU driver updates; the cloud dies to rate limits
and outages. They rarely die together. So the manager supports a fallback
chain (`LLM_FALLBACK_PROVIDER`), with two deliberate properties:

1. **Strictly opt-in.** If you chose Ollama specifically so your titles stay
   on your network, silently shipping prompts to Google on a hiccup would be
   a betrayal, not a feature. No fallback happens unless configured.
2. **The primary wins back automatically.** The manager keeps probing a
   downed primary while the fallback covers, and switches back the moment it
   recovers — coming home to find you're still paying Google two weeks after
   the local box came back would be its own kind of annoying.

Deduplication state lives on the *manager*, not a provider, so failing over
doesn't wipe the memory of what you just posted.

## Guardrails: fact-check the robot

Small local models (the 4B–12B class this library targets) reliably fail in
the same ways: they invent details, pad with filler, and wrap answers in
meta-chatter. The guardrails are tuned to those failure modes:

- **Hallucination patterns are per-domain** (`ContentProfile`). A stream
  announcement inventing "VOD coming soon" is a different lie than a video
  announcement inventing "live now", and a starred-repo post inventing
  "50k stars" is a uniquely embarrassing way to be wrong in public. Rather
  than fork the guardrails per daemon, they're parameterized — and one
  hallucination match rejects the message outright, because posting
  "giveaway tonight at 8pm" when there is no giveaway is worse than posting
  nothing.
- **Hard checks vs. soft scores.** Fabrications, wrong hashtag counts, and
  inline URLs are hard failures. Style issues (generic phrases, weak
  title relevance) only lower a score, and scoring is off by default —
  strictness there is the caller's choice. Stream-daemon, for instance,
  deliberately ships a message with minor style issues rather than staying
  silent, and uses the individual `guardrails.*` functions to build its own
  lenient retry flow.
- **URLs are stripped from content and appended after validation** — links
  belong in facets/embeds, and a model-invented URL should never survive.

## Config: two chains, different priorities

Plain settings and secrets deliberately resolve differently:

- **Settings** prefer the local environment (simple key, then sectioned) so
  a developer's shell wins during debugging.
- **Secrets** prefer the manager (Doppler → AWS → Vault → env) so a
  production credential rotation genuinely overrides whatever stale value a
  `.env` file still carries. This ordering fixed a real bug that
  stream-daemon's own test suite had been flagging.

Both chains treat empty strings and `YOUR_*` template placeholders as unset,
and a secrets manager being unreachable degrades to env lookups instead of
crashing.

## Explicit event kinds

Discord embeds used to infer upload-vs-live from the platform name, and
"youtube" is ambiguous: Boon-Tube means a new upload, stream-daemon means a
live broadcast. Guessing gave one of them the wrong embed. Now the caller
states `event_kind` in the payload, and a platform default
(`DiscordPlatform(default_event_kind=...)`) only applies when the payload
doesn't say. The general rule: **when a value is ambiguous at the callee,
make the caller say it.**

## Versioning and compatibility

- Public API is everything documented in [API.md](API.md); `_`-prefixed
  names may change without notice.
- Semantic versioning; while on `0.x`, minor bumps may break, so daemons pin
  `>=0.1,<0.2` style ranges.
- Python 3.9+ — CI runs the matrix through 3.13, plus a bare-install job
  and a build/`twine check` gate, with all actions pinned by SHA.
