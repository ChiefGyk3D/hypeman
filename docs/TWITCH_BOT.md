# Twitch chat bot

`TwitchChat` lets a daemon hear a Twitch channel and answer in it. It is an
adapter only: it converts chat messages into `ChatEvent`s and sends text. Who
the bot is, when it speaks and what it may say belong to the consumer.

```python
from hypeman_social.social import CHAT_REGISTRY

chat = CHAT_REGISTRY['twitch']()
if chat.is_available():
    chat.start(lambda event: print(event.author_id, event.text))
    chat.send('your_channel', 'hello chat')
```

## Why the bot is not the broadcaster's account

A chat bot holds a token that can type in chat. If that token is the
broadcaster's, a leak lets an attacker speak as the broadcaster, with the
broadcaster's moderation powers, on the channel that matters most. A separate
account limits the damage to one muted, removable, replaceable user. It also
lets `is_self` tell the bot's own messages from the broadcaster's, and Twitch's
documented Chat Bot Badge requires that the bot account is not the channel's
broadcaster.

## Set the bot account up

1. Create a new Twitch account for the bot with its own email address.
   Do not reuse the broadcaster's email.
2. Turn on two-factor authentication for that account.
3. Log in as the bot and register an application in the Twitch developer
   console. Choose a **confidential** client type, set an OAuth redirect URL you
   control (for a one-time token run, `http://localhost:3000` is enough), and
   note the Client ID and generate a client secret.
4. Request the minimum scopes, authorising as the bot account through the
   authorization code flow (`https://id.twitch.tv/oauth2/authorize`, then the
   code exchange at `https://id.twitch.tv/oauth2/token`):
   - `user:read:chat` to receive chat messages
   - `user:write:chat` to send chat messages

   Do not request more. `user:bot` and `channel:bot` are only needed for the
   app-access-token path and the Chat Bot Badge, which this adapter does not use.
5. Find the bot's numeric user id (the `user_id` in the validate response, or
   the Get Users endpoint).
6. Put the values in Doppler (the only secret store this adapter is designed
   around; see [Configuration](CONFIGURATION.md#chat-twitch)). Never put them in code, a
   committed file or a chat log.

### How the broadcaster lets the bot in

With a user access token, the bot reads and writes chat as itself. A bot that is
not a moderator is subject to Twitch's ordinary chat rules for the channel
(follower-only, slow mode, and so on), and the broadcaster can make the bot a
moderator or VIP to lift them. Twitch returns 403 when the bot is banned or timed
out. The broadcaster does not hand over any token.

## Token handling

* At start, on a schedule of 55 minutes (Twitch requires hourly) and whenever
  the expiry Twitch reported is within 15 minutes, the token is checked against
  `https://id.twitch.tv/oauth2/validate`. The adapter keeps the absolute expiry
  deadline from that answer and never treats the token as usable past it, even
  while validation is failing for transient reasons. It also checks that the
  token belongs to the configured application and to `bot_user_id` and that it
  carries both scopes.
* A 401, or a token close to expiry, triggers a refresh with the refresh token.
  A replacement access token is staged and only handed to senders after it has
  passed validation; a replacement that fails validation (for example a token
  for another user) is never used to send.
* If the refresh fails, the adapter becomes unavailable and logs one error. It
  tries once more every 15 minutes. That retry invalidates the cached secrets
  (`reset_secret_cache()`, a re-fetch of the Doppler project) and re-reads them,
  so correcting the values in Doppler heals it without a restart. It never
  retries in a loop.
* **Refresh tokens.** Twitch documents that a refresh token *may* change when it
  is used, and that the application should store the replacement. The adapter
  always keeps the newest pair in memory and uses it for the next refresh. On
  recovery it adopts the stored pair only if the stored values differ from the
  ones it read last time (the operator replaced them); an unchanged, older
  stored pair never overwrites a newer one held in memory.
* **Persisting a replacement.** Writing back to Doppler would need a Doppler
  token with write access, which this adapter deliberately does not ask for and
  has no code path for. Pass `on_token_refresh=callback(access_token,
  refresh_token)` to `TwitchChat(...)` if the consumer wants to persist the new
  pair somewhere secure. It runs after each successful refresh, outside the
  adapter's lock; an exception from it is swallowed and logged by type only, and
  the values are never logged by the adapter.
* **What a restart does.** Without a callback the replacement lives in memory
  only, so a restart reads the stored pair again. Whether the old refresh token
  still works after Twitch has issued a replacement is not something I could
  confirm in the documentation, so after a long uptime, or after a log line
  saying the tokens were refreshed, treat an unavailable bot at startup as a
  reason to regenerate the stored tokens (see Revoke and rotate).
* Nothing logs a token, a client secret or an `Authorization` header, and
  exception messages are logged by type only. Sanitised internal exceptions are
  raised outside the `except` block so they carry no `__context__` with the
  original text. Tests enforce this.

## Receiving: what is accepted

* Only `channel.chat.message` notifications for the configured channel.
* **Shared chat.** In a shared chat session Twitch delivers other participants'
  messages to this channel with `source_broadcaster_user_id` set to the channel
  they came from. These are dropped (not delivered with `bridged=False` and the
  origin discarded). The choice is deliberate: the bot's conversation is the
  configured channel's.
* **Replay and duplicates.** Notifications whose `message_timestamp` is missing
  or older than ten minutes (Twitch's own guidance for replay protection) are
  dropped, and `message_id` values are remembered for that window, bounded by
  time and by size (4096 ids). Delivery from Twitch is at-least-once, and the
  id cache cannot cover a very busy channel forever, so **consumers should be
  idempotent by `ChatEvent.message_id`**. If the host clock is more than ten
  minutes fast, every message looks old and is dropped; one warning says so.
* **Frame size.** A frame, or a fragmented message in total, over 1 MiB of
  encoded bytes is refused as soon as its header declares the length (a
  `websocket-client` subclass checks the length before the payload is read, and
  the connection is dropped and re-established). Residual: an accepted 1 MiB
  frame is buffered in memory, roughly two to four times that transiently while
  it is joined and decoded. If `websocket-client` changes the internals this
  relies on, the adapter falls back to the stock reader plus a byte-count check
  after the whole frame has been read, which does not bound allocation.
* **Connections.** Handshake redirects are not followed. A reconnect URL must be
  `wss://eventsub.wss.twitch.tv`, port absent or 443, no user information, path
  empty or `/ws` (the forms the EventSub documentation shows).

## Revoke and rotate

If a token or secret may have leaked, or the bot is misbehaving:

1. **Stop the daemon** (or call `stop()`).
2. As the bot account, disconnect the application at
   `https://www.twitch.tv/settings/connections`. This invalidates its tokens.
3. In the developer console, **generate a new client secret** for the
   application (this invalidates the old one). If the Client ID itself was
   exposed, register a new application.
4. Run the authorization code flow again as the bot to get a new access token
   and refresh token.
5. Update `client_secret`, `access_token` and `refresh_token` in Doppler, and
   rotate anything else that sat beside them.
6. Start the daemon. `is_available()` re-reads the secrets and validates before
   the bot speaks again.

To remove the bot entirely, ban or remove the account in the channel and delete
the application.

## What was and was not verified

Read from dev.twitch.tv while writing this: the EventSub WebSocket handshake
(welcome, keepalive, reconnect, revocation, close codes), the reconnect URL form
(`wss://eventsub.wss.twitch.tv?...`), the ten-minute replay guidance and
`message_id` de-duplication on the EventSub overview, the
`channel.chat.message` event fields, `reply.parent_message_id` and the
`source_broadcaster_user_id` shared-chat fields, Send Chat Message (endpoint,
body, 500 character limit, `is_sent` and `drop_reason`, status codes, and the
shared-chat behaviour per token type), token validation and its hourly
requirement, token refresh (including that refresh tokens may change), the scope
names, and the chat rate limits.

Not verified: the exact badge `set_id` strings (`broadcaster`, `moderator`,
`subscriber`, `vip` are used; the pages I read gave only `bits` and `subscriber`
as examples); whether the 500 limit counts code points or UTF-16 units;
Twitch's duplicate-message rules; whether Twitch treats other leading
characters than `/` and `.` as commands; and the behaviour of an old refresh
token after a replacement is issued. Nothing here was run against live Twitch.
