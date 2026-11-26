# Matrix: password to access token

Matrix authentication hardening is tracked in issue #144. The library now
prefers `MATRIX_ACCESS_TOKEN` whenever it is set. `MATRIX_USERNAME` and
`MATRIX_PASSWORD` still work as a fallback, so nothing breaks, but a password
login now happens once per process, the session is reused, and it is logged out
(`POST /_matrix/client/v3/logout`) on `close()` or exit. Move each consumer to a
token so the account password is not used on every start.

Everything below uses placeholders. Substitute your own values.

## Consumers

Stream-Daemon, Boon-Tube-Daemon, boon-tube-llm, Star-Daemon, star-daemon-llm,
yomama-as-a-service and Patch-Gremlin all use the password today. typo-sniper
is already token only. Each consumer pins this library, so it needs a pin bump
to the release that contains this change. New releases are held 7 days before
merging, so plan the bump for after that window. Until the bump lands, the
consumer keeps the old behavior and the steps below must wait.

## Steps (per consumer)

1. **Dedicated bot account.** Create or reuse one Matrix account per app, for
   example `@app-bot:matrix.example.org`, invited only to the room it posts in.
   Do not share one account between apps.
2. **Log in once, out of band.** Request a token from the homeserver with the
   login endpoint, from a trusted shell. Keep the token out of shell history,
   argv and the terminal. Read the password from a prompt and write the token
   straight to the secret manager over stdin, with output discarded:

   ```bash
   read -rs -p "Password: " MX_PW; echo
   curl -s -X POST "https://matrix.example.org/_matrix/client/v3/login" \
     -H 'Content-Type: application/json' \
     -d @- <<JSON | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"], end="")' \
     | secret-manager-set APP_MATRIX_ACCESS_TOKEN > /dev/null
   {"type":"m.login.password","identifier":{"type":"m.id.user","user":"app-bot"},"password":"$MX_PW","initial_device_display_name":"app-bot token"}
   JSON
   unset MX_PW
   ```

   `secret-manager-set` stands for your secret manager's "set from stdin"
   command. Whatever you use must read the value from stdin and its output
   must go to `/dev/null`, because some tools echo the value they store.
3. **Set `MATRIX_ACCESS_TOKEN`** in the consumer's secret store (or the secret
   name the consumer already maps for Matrix).
4. **Remove `MATRIX_PASSWORD` and `MATRIX_USERNAME`** from the consumer's
   environment and secret store.
5. **Verify with one test post** to the room. Confirm the message arrives and
   the daemon log shows `Using static access token authentication`, with no
   login line.
6. **Revoke old devices.** In the bot account's session list, sign out every
   device that is not the one you just created. This clears sessions left by
   earlier password logins.
7. **Rollback.** Restore `MATRIX_USERNAME` and `MATRIX_PASSWORD`, remove
   `MATRIX_ACCESS_TOKEN`, restart. The library falls back to password login.
   If the token itself is bad, remove it and rotate: log in again (step 2) and
   store the new value.

## Notes

- The library never logs the token or the password.
- A token created by a password login inside the library is not stored: it
  lives in memory for the process and is logged out on shutdown.
- A static token is never logged out by the library. Revoke it from the
  account's session list when you retire it.
