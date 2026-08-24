# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Health and status reporting.

The reason the AI outage went unnoticed for so long is that neither daemon
could tell you anything about itself while running. It kept posting — with
template fallbacks instead of generated messages — and looked fine from the
outside. You found out by noticing the posts had changed character.

This gives each daemon a state object it updates as it works, optionally
exposed over HTTP:

    GET /healthz   200 if the daemon is alive and its core platforms work,
                   503 if something essential is broken. Body is JSON either way.
    GET /status    Always 200. Full detail: every platform, the LLM, last poll,
                   last post, uptime.

The distinction matters: 'the AI server is down' should NOT make /healthz fail,
because the daemon is still doing its job with fallback messages. Degraded is
not dead, and a health check that cries wolf gets ignored.
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class HealthState:
    """
    Mutable snapshot of what a daemon is doing. Thread-safe.

    Register components (social platforms, the LLM, media pollers) and update
    them as things happen. The daemon owns one of these; the HTTP server, if
    enabled, just reads it.
    """

    def __init__(self, service_name: str):
        self.service_name = service_name
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._components: Dict[str, Dict[str, Any]] = {}
        self._events: Dict[str, Any] = {}
        self._status_providers: Dict[str, Callable[[], Dict[str, Any]]] = {}

    # ─────────────────────────────────────────────────────────────────────
    # Recording
    # ─────────────────────────────────────────────────────────────────────

    def register(self, name: str, provider: Callable[[], Dict[str, Any]]) -> None:
        """
        Register a live status source.

        The provider is called on each status read, so it always reflects
        current state — important for the LLM, whose availability changes
        underneath us.

        Args:
            name: Component name, e.g. 'llm', 'bluesky'.
            provider: Zero-arg callable returning a JSON-serialisable dict.
        """
        with self._lock:
            self._status_providers[name] = provider

    def set_component(self, name: str, healthy: bool, detail: Optional[str] = None) -> None:
        """Record a component's health directly, for things without a provider."""
        with self._lock:
            self._components[name] = {
                'healthy': healthy,
                'detail': detail,
                'updated_at': _now_iso(),
            }

    def record_event(self, name: str, detail: Optional[Any] = None) -> None:
        """
        Note that something happened, with a timestamp.

        Use for 'last_poll', 'last_post', 'last_error' — the things you want to
        see when asking "is this daemon actually working, or just running?"
        """
        with self._lock:
            self._events[name] = {'at': _now_iso(), 'detail': detail}

    # ─────────────────────────────────────────────────────────────────────
    # Reporting
    # ─────────────────────────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        """Full current state as a JSON-serialisable dict."""
        with self._lock:
            providers = dict(self._status_providers)
            components = dict(self._components)
            events = dict(self._events)
            started = self._started_at

        live = {}
        for name, provider in providers.items():
            try:
                live[name] = provider()
            except Exception as e:
                live[name] = {'error': f"{type(e).__name__}: {e}"}

        return {
            'service': self.service_name,
            'healthy': self.is_healthy(),
            'uptime_seconds': int(time.time() - started),
            'started_at': datetime.fromtimestamp(started, timezone.utc).isoformat(),
            'components': {**components, **live},
            'events': events,
        }

    def is_healthy(self) -> bool:
        """
        True if nothing essential is broken.

        Only components explicitly marked unhealthy count against this.
        Provider-supplied status (the LLM in particular) is reported but does
        not fail the check — a daemon posting template messages because the AI
        box is offline is degraded, not down.
        """
        with self._lock:
            return all(c.get('healthy', True) for c in self._components.values())


def _now_iso() -> str:
    """Current UTC time, ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


class _HealthHandler(BaseHTTPRequestHandler):
    """Tiny read-only HTTP handler. No routing library, no dependencies."""

    state: Optional[HealthState] = None

    def do_GET(self):  # noqa: N802 — name fixed by BaseHTTPRequestHandler
        if self.state is None:
            self._respond(503, {'error': 'health state not attached'})
            return

        if self.path.rstrip('/') in ('/healthz', '/health'):
            healthy = self.state.is_healthy()
            self._respond(
                200 if healthy else 503,
                {'service': self.state.service_name, 'healthy': healthy},
            )
        elif self.path.rstrip('/') in ('/status', ''):
            self._respond(200, self.state.snapshot())
        else:
            self._respond(404, {'error': 'not found'})

    def _respond(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2, default=str).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """Silence per-request stdout logging; that's what we're trying to reduce."""
        logger.debug(f"health request: {format % args}")


def start_health_server(
    state: HealthState,
    port: int,
    host: str = '127.0.0.1',
) -> Optional[HTTPServer]:
    """
    Serve the health endpoints on a background daemon thread.

    Binds to localhost by default. This endpoint reports which platforms are
    configured and whether credentials work — useful to you, and a free recon
    gift to anyone else. Bind to 0.0.0.0 only behind something that controls
    who can reach it.

    Args:
        state: The HealthState to serve.
        port: TCP port. 0 disables the server entirely.
        host: Interface to bind.

    Returns:
        The running server, or None if disabled or the bind failed.
    """
    if not port:
        return None

    try:
        handler = type('BoundHealthHandler', (_HealthHandler,), {'state': state})
        server = HTTPServer((host, port), handler)
    except OSError as e:
        # A busy port must never stop the daemon from doing its actual job.
        logger.error(f"✗ Could not start health server on {host}:{port}: {type(e).__name__}: {e}")
        return None

    thread = threading.Thread(
        target=server.serve_forever,
        name='hypeman-health',
        daemon=True,
    )
    thread.start()
    logger.info(f"✓ Health endpoint at http://{host}:{port}/healthz (and /status)")
    return server
