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
Message History** and **Send Messages**. Nothing else: no Administrator, no
Manage anything, no Mention Everyone. Replies are also sent with
`allowed_mentions` empty, so echoed text can never ping `@everyone`, a role
or a user. Restrict it further with `DISCORD_GUILD_ID` and
`DISCORD_CHANNEL_IDS`.

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
  reported by `GUILD_CREATE`. Nothing is guessed from names or permissions.

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
ellipsis. 429 responses are retried after `retry_after`; an exhausted
bucket (`X-RateLimit-Remaining: 0`) delays the next send to that channel.
Waiting is capped at 30 seconds per call, then `send()` returns False. It
never raises. `channel` must be a numeric channel id.

## 6. Connection behaviour

Heartbeats follow Discord's rules, a missing ACK is treated as a dead
connection and resumed, `Reconnect` and resumable closes resume, and an
invalid session starts a fresh identify. Failed connections back off from 1
second up to 5 minutes (Discord limits Identify to 1000 per day). Closes that
cannot be fixed by reconnecting (bad token 4004, invalid intents 4013, missing
privileged intent 4014, and similar) stop the receiver.

## 7. Rotating or revoking the token

Developer Portal, your application, Bot page, **Reset Token**. The old token
dies at once. Put the new one in Doppler and restart the process (or call
`stop()` then `start()`). The token is never logged and is scrubbed from any
error text; if you suspect it leaked, reset it first and investigate after.
To revoke access entirely, kick the bot from the guild or delete the
application.

## References

Discord documentation: Gateway (`docs.discord.com/developers/events/gateway`),
Rate Limits (`docs.discord.com/developers/topics/rate-limits`), Message
resource (`docs.discord.com/developers/resources/message`).
