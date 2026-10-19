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
- **Receive limits.** A frame or reassembled message over 2 MiB (encoded
  bytes, `MAX_MESSAGE_BYTES`) is refused and the connection is closed (1009)
  and resumed. With websocket-client's frame buffer the length field is checked
  right after the header, before the payload is read, and the cap on a
  fragmented message shrinks as fragments arrive. Residual: if a future
  websocket-client renames that internal hook, the guard falls back to a check
  after each frame is read, so one frame up to its declared length is buffered
  first. A legitimate `GUILD_CREATE` for an enormous guild larger than 2 MiB
  would be refused repeatedly; raise the constant if that is you.
- **Identify limits.** At startup the bot reads `GET /gateway/bot` for
  `session_start_limit` and `max_concurrency` (and the gateway URL). Identifies
  are spaced 5 seconds divided by `max_concurrency`. The bot keeps a rolling
  24-hour count of its own Identifies and a reserve of 20: when the count (or
  Discord's reported `remaining`) reaches the reserve it stops with one error
  instead of burning the allowance, because hitting the daily limit resets the
  token. Failure backoff runs from 1 second to 5 minutes and is only reset after
  a connection stayed up 60 seconds past READY or RESUMED, so a loop of
  Identify, READY, session lost cannot spin.
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
