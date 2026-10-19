# Discord chat (receiving)

`DiscordChat` lets a conversational bot hear Discord guild channels and reply.
It is separate from the webhook sender in `social/discord.py`, which keeps
working unchanged. Install the extra:

```bash
pip install "hypeman-social[discord-chat]"    # adds websocket-client
```

## 1. Create the bot application

1. Discord Developer Portal, New Application, then the **Bot** page, Add Bot.
2. Copy the token once and store it in Doppler (or your secret manager) as
   `DISCORD_BOT_TOKEN`. Do not put it in a file in the repository.
3. On the same Bot page, under **Privileged Gateway Intents**, enable
   **Message Content Intent**. Without it Discord closes the gateway with
   code 4014; `DiscordChat` stops, logs a message that names this switch, and
   `is_available()` returns False until you fix it and call `start()` again.
   (Bots in 100 or more guilds also need Discord's approval for this intent.)
4. Install the bot with the OAuth2 URL generator: scope `bot` only.

## 2. Least-privilege permissions

The bot needs, in the channels it should serve: **View Channel**, **Read
Message History**, **Send Messages**, and **Send Messages in Threads** if it
should answer in threads (Discord does not inherit Send Messages into threads;
a forum post is a thread). Nothing else: no Administrator, no
Manage anything, no Mention Everyone. Replies are also sent with
`allowed_mentions` empty, so echoed text can never ping `@everyone`, a role
or a user. Restrict it further with `DISCORD_GUILD_ID` and
`DISCORD_CHANNEL_IDS`.

Filters: an **absent** (or blank) filter means unrestricted, by design. A filter
that is **set but invalid** (not a list of ASCII-digit ids) fails closed: the
adapter reports itself unavailable, logs one error naming the setting (never
the value), and hears nothing until you fix it. `DISCORD_CHANNEL_IDS` matches
exact channel ids, and a thread is its own channel: a message in a thread or a
forum post carries the thread's id, so to hear a forum post list its thread id
(a parent channel id does not cover its threads).

Configuration keys are in [CONFIGURATION.md](CONFIGURATION.md#chat-discord-receiving).

## 3. What it hears

- Guild text-channel messages of type default or reply. Joins, pins and other
  system messages are ignored, as are messages with no text.
- **Direct messages are ignored entirely** (an event with no `guild_id` is
  dropped). The bot never reads private conversations.
- `author_id` is the Discord user id as a string, never the display name.
- `is_self` is True when the author id equals the bot's own user id (learned
  from the Gateway `READY` event).
- Roles: `moderator` if the member holds a role listed in
  `DISCORD_MODERATOR_ROLE_IDS`; `broadcaster` if the author is the guild owner
  reported by the Gateway. The owner is tracked through `GUILD_CREATE` and
  `GUILD_UPDATE`, forgotten on `GUILD_DELETE` or an unavailable guild, and
  rebuilt from scratch on every fresh session, so a transferred guild stops
  granting `broadcaster` to the old owner. An unknown owner grants nothing.
  Nothing is guessed from names or permissions.

## 4. Bridged messages

If a bridge (for example a mautrix Discord/Matrix bridge) relays another
network into a Discord channel, those messages arrive as Discord messages
sent by something other than the person. `ChatEvent.bridged` is True when:

- the message has a `webhook_id` (the usual way a bridge relays a foreign
  user), or
- the author is a bot account that is **not** this bot (a ghost or puppet).

The raw hints are in `extra`: `webhook_id` (only when present),
`author_bot` (`'true'` or `'false'`), `author_username`, `guild_id`.

What `bridged` does **not** guarantee:

- It does not identify the original sender. For a webhook relay the Discord
  author is the webhook, so `author_id` is the webhook's id, and the real
  name is usually only in the display name, which anyone with the bridge can
  set. Treat a bridged sender as lower trust than a native one.
- Any other bot in the guild is also `bridged=True`. The flag means "not typed
  by a person on Discord", not "came from Matrix".
- It does not prove a message is *not* bridged: a bridge that posts as a
  normal user account (a self-bot) looks native.
- Bridged messages never carry roles, so a ghost account that happens to hold
  the moderator role cannot grant that trust to a relayed sender.

These are assumptions about how a bridge behaves. **Verify them against your
own mautrix deployment** (send a message from the Matrix side and inspect the
resulting `MESSAGE_CREATE`) before anything relies on them. The echo of the
bot's own reply coming back through the bridge is dropped by `is_self` only
if the bridge relays it under the bot's own Discord id; the consumer should
also dedupe by message id and drop the bridge's ghost user for the bot.

## 5. Sending

`send(channel, text, reply_to_id)` posts through the REST API with the bot
token. Text over 2000 characters (Discord's limit) is truncated with an
ellipsis. It never raises. `channel` must be a numeric channel id.

- **No duplicate replies.** Each `send()` carries a `nonce` with
  `enforce_nonce`, which Discord documents for Create Message: a repeat from
  the same author returns the first message instead of creating another. That
  makes the retry after a 5xx safe. A transport error or timeout (the outcome
  is unknown) is not retried at all.
- **Rate limits are shared.** A 429 records its deadline: a global one holds
  back every channel, a per-bucket one holds back that channel. Sends to one
  channel go one at a time, so the bucket state from a response is known before
  the next caller is admitted. An exhausted bucket
  (`X-RateLimit-Remaining: 0`) delays the next send. Waiting is capped at 30
  seconds per call, then `send()` returns False. A non-finite `retry_after` is
  rejected, not slept on.

## 6. Connection behaviour

Heartbeats follow Discord's rules; a missing ACK is treated as a dead
connection and resumed. `Reconnect` and resumable closes resume, closing with a
status other than 1000 or 1001 (Discord invalidates the session on those two),
and an invalid session starts a fresh identify. Closes that cannot be fixed by
reconnecting (bad token 4004, invalid shard 4010, sharding required 4011,
invalid API version 4012, invalid intents 4013, missing privileged intent 4014)
stop the receiver.

- **The token only goes to Discord.** The gateway URL and every
  `resume_gateway_url` must be `wss`, host `gateway.discord.gg` or a subdomain of
  `discord.gg` (the documented regional resume hosts), port 443 or none, no
  userinfo or fragment. Anything else is ignored and the bot Identifies at the
  default URL instead. Handshake redirects are disabled. I could not confirm in
  Discord's docs that this host set is exhaustive for `resume_gateway_url`; if
  Discord ever hands out another domain the bot will log an untrusted-URL error
  and fall back to a fresh Identify rather than send the token there.
- **Receive limits.** A frame or reassembled message over `DISCORD_MAX_EVENT_BYTES`
  (encoded bytes; default 2 MiB, at most 64 MiB) is refused. The check runs right
  after a frame header is read, before the payload is allocated, and counts
  fragments cumulatively. It leans on websocket-client internals, so the adapter
  **probes** them on an in-process socket pair before it will start: if the hook
  is missing, renamed, or does not refuse an oversize frame, `is_available()` is
  False with one error naming the installed websocket-client version, and no
  connection is made with the stock reader (supported: `websocket-client>=1.6.0,<2`).
  An event over the cap is a **stopped condition**, not a retry: Discord would
  replay it on every Resume and a fresh session would burn Identifies, so the
  receiver goes unavailable with one error that names the setting. Raise
  `DISCORD_MAX_EVENT_BYTES` (for a very large guild's `GUILD_CREATE`) and call
  `start()`. A value that is not a positive integer up to the ceiling is an
  invalid setting and fails closed like the filters.
- **Identify limits.** Before every fresh Identify the bot asks `GET /gateway/bot`
  and needs a complete, valid `session_start_limit` (`total`, `remaining`,
  `reset_after`, `max_concurrency`). Without one it does not Identify: it retries
  with the 1 second to 5 minute backoff (a Resume of a held session needs no
  lookup). Identifies are always 5 seconds apart, whatever `max_concurrency` is:
  an unsharded client is always in bucket 0 (`shard_id % max_concurrency`), so
  that value is no licence to go faster. The bot also keeps a rolling 24-hour
  count of its own Identifies and a reserve of 20: when the count (or Discord's
  reported `remaining`) reaches the reserve it stops with one error instead of
  burning the allowance, because hitting the daily limit resets the token. The
  failure backoff is only reset after a connection stayed up 60 seconds past
  READY or RESUMED, so a loop of Identify, READY, session lost cannot spin.
- **Configuration is read as one snapshot.** Every setting is parsed first and
  published together, so a reload (`is_available()` re-reads) can never leave a
  filter half-applied. A session belongs to the credential that created it: after
  a reload with a different token the bot Identifies fresh and never Resumes the
  old credential's session.
- **Foreign code is fenced.** A handler that raises anything, `BaseException`
  included, is logged by class name only and the receiver carries on with the next
  event. If the worker itself dies, `is_available()` reports it (and `start()`
  restarts it); nothing reaches Python's thread excepthook, which would print the
  exception with its chained context.
- **Session-keeping close.** websocket-client acknowledges a server close frame
  itself, with status 1000, which Discord treats as ending the session. The
  adapter's reader sends 4000 instead while a session is held, and 1000 on a
  real shutdown.
- **Never enable websocket-client's trace** (`websocket.enableTrace(True)`) in
  production: it logs raw frames, including the Identify payload with the bot
  token.
- **Stopping.** `stop()` ends the worker and, if it is blocked, closes the
  socket. `shutdown()` does the same and returns False (with an error log) if
  the worker still did not exit; `start()` refuses while any worker is alive, so
  two receivers never run together. Cancellation is checked after every
  blocking call, so a stop in progress never sends Identify or delivers a
  message.

## 7. Rotating or revoking the token

Developer Portal, your application, Bot page, **Reset Token**. The old token
dies at once. Put the new one in Doppler and call `start()` again (or restart
the process). `start()` clears the stopped state left by a fatal close, re-reads
the credentials and filters, then checks availability, so it is the recovery
path after a 4004; `is_available()` alone keeps returning False until then.
The token is never logged. Transport errors are logged by exception type
only, never their text, so neither the old nor the new token can leak through
one; if you suspect a leak, reset the token first and investigate after.
To revoke access entirely, kick the bot from the guild or delete the
application.

## References

Discord documentation: Gateway (`docs.discord.com/developers/events/gateway`),
Rate Limits (`docs.discord.com/developers/topics/rate-limits`), Message
resource (`docs.discord.com/developers/resources/message`).
