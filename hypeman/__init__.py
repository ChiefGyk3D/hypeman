# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
hypeman — the shared core behind ChiefGyk3D's announcement daemons.

A hype man's entire job is announcing you loudly to a crowd. That's what these
daemons do: Boon-Tube-Daemon shouts when you post a video, stream-daemon shouts
when you go live, Star-Daemon shouts when you star a repo. They were three
copies of the same code, drifting apart, and a bug fixed in one never reached
the others. Now they share this.

What's in here:

    hypeman.config          Config and secrets (env, .env, AWS, Vault, Doppler)
    hypeman.llm             Ollama + Gemini, guardrails, automatic failover
    hypeman.social          Bluesky, Mastodon, Discord, Matrix
    hypeman.observability   Logging with rotation, health endpoints

Nothing in here knows what you're announcing. That's the caller's business.
"""

__version__ = '0.1.0'

__all__ = ['__version__']
