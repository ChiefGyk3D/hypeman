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

* At start, and on a schedule of 55 minutes (Twitch requires hourly), the token
  is checked against `https://id.twitch.tv/oauth2/validate`. The adapter checks
  that it belongs to the configured application and to `bot_user_id` and that it
  carries both scopes.
* A 401, or a token close to expiry, triggers a refresh with the refresh token.
  If the refresh fails, the adapter becomes unavailable and logs one error. It
  tries once more every 15 minutes, re-reading the secrets, so correcting the
  values in Doppler heals it without a restart. It never retries in a loop.
* **A refreshed token is held in memory only.** Writing it back would need a
  Doppler token with write access, which this adapter deliberately does not ask
  for. After a restart the stored tokens are read again. Twitch rotates refresh
  tokens on every refresh; whether the previous refresh token stays usable is not
  stated in the documentation I could read, so treat a restart after a long
  uptime as a reason to regenerate the stored tokens if the bot comes up
  unavailable.
* Nothing logs a token, a client secret or an `Authorization` header, and
  exception messages are logged by type only. A test enforces this.

## Limits the adapter applies

* Messages are collapsed to one line and cut at 500 characters (the documented
  Send Chat Message limit).
* A local limiter allows 15 messages per 30 seconds and one every 1.5 seconds.
  Twitch allows 20 per 30 seconds and one per second per channel for a sender
  that is not the broadcaster, a moderator or a VIP, and ignores a bot that
  exceeds them for an hour. When the limiter says no, `send()` returns `False`
  at once; it never waits. A 429 response pauses sending for up to a minute.
* The bot only sends to the channel it was configured for.

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
(welcome, keepalive, reconnect, revocation, close codes), the
`channel.chat.message` event fields and `reply.parent_message_id`, Send Chat
Message (endpoint, body, 500 character limit, `is_sent` and `drop_reason`,
status codes), token validation and its hourly requirement, token refresh, the
scope names, and the chat rate limits.

Not verified: the exact badge `set_id` strings (`broadcaster`, `moderator`,
`subscriber`, `vip` are used; the pages I read gave only `bits` and `subscriber`
as examples); whether the 500 limit counts code points or UTF-16 units;
Twitch's duplicate-message rules; and the behaviour of an old refresh token
after rotation. Nothing here was run against live Twitch.
