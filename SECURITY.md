# Security Policy

## Supported versions

hypeman-social is pre-1.0 and released continuously from `main`. Security
fixes are made against the latest release on PyPI; there are no separate
long-term-support branches.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for a security vulnerability.

Report it privately through
[GitHub Security Advisories](https://github.com/ChiefGyk3D/hypeman/security/advisories/new)
for this repository. That opens a private discussion with the maintainer and
lets us coordinate a fix and a disclosure timeline before details become
public.

Include, where you can:

- A description of the vulnerability and its potential impact
- Steps to reproduce it, or a proof of concept
- The affected version (`pip show hypeman-social` or the commit SHA)

## What counts

This project generates and posts social media content and handles platform
credentials (Bluesky app passwords, Mastodon tokens, Discord webhook URLs,
Matrix access tokens, LLM API keys). Reports of particular interest:

- Credentials or secrets logged, persisted, or otherwise exposed in clear
  text (see `hypeman_social/config/secrets.py` and the `_redact`-style
  patterns used across the `social/` and `llm/` modules)
- Prompt injection that causes the LLM layer to post something the operator
  did not intend
- SSRF or arbitrary outbound requests driven by untrusted input

## Response

We aim to acknowledge new reports within 7 days and to keep the reporter
updated as a fix is developed. Coordinated disclosure is appreciated;
give us a reasonable window to ship a patched release before publishing
details.
