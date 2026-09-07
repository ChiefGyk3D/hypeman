# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Health state and the HTTP endpoints that expose it."""

import json
import urllib.error
import urllib.request

from hypeman_social.observability.health import HealthState, start_health_server


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, json.loads(response.read())


class TestHealthState:
    def test_healthy_by_default(self):
        assert HealthState('testd').is_healthy() is True

    def test_unhealthy_component_fails_check(self):
        state = HealthState('testd')
        state.set_component('bluesky', healthy=False, detail='auth expired')
        assert state.is_healthy() is False

    def test_recovery_restores_health(self):
        state = HealthState('testd')
        state.set_component('bluesky', healthy=False)
        state.set_component('bluesky', healthy=True)
        assert state.is_healthy() is True

    def test_snapshot_merges_providers_and_events(self):
        state = HealthState('testd')
        state.register('llm', lambda: {'available': True})
        state.set_component('discord', healthy=True)
        state.record_event('last_post', detail='twitch')

        snap = state.snapshot()
        assert snap['service'] == 'testd'
        assert snap['components']['llm'] == {'available': True}
        assert snap['components']['discord']['healthy'] is True
        assert snap['events']['last_post']['detail'] == 'twitch'
        assert snap['uptime_seconds'] >= 0

    def test_broken_provider_reports_error_not_crash(self):
        state = HealthState('testd')

        def explode():
            raise RuntimeError('provider bug')

        state.register('llm', explode)
        snap = state.snapshot()
        assert 'RuntimeError' in snap['components']['llm']['error']

    def test_degraded_llm_does_not_fail_health(self):
        """The core contract: AI down = degraded, not dead."""
        state = HealthState('testd')
        state.register('llm', lambda: {'available': False})
        assert state.is_healthy() is True


class TestHealthServer:
    def test_disabled_when_port_zero(self):
        assert start_health_server(HealthState('testd'), port=0) is None

    def test_endpoints(self):
        state = HealthState('testd')
        server = start_health_server(state, port=_free_port())
        try:
            base = f'http://127.0.0.1:{server.server_address[1]}'

            code, body = _get(base + '/healthz')
            assert code == 200 and body['healthy'] is True

            state.set_component('discord', healthy=False)
            try:
                _get(base + '/healthz')
                raise AssertionError('expected HTTP 503')
            except urllib.error.HTTPError as e:
                assert e.code == 503

            code, body = _get(base + '/status')
            assert code == 200
            assert body['service'] == 'testd'
            assert body['healthy'] is False

            try:
                _get(base + '/nope')
                raise AssertionError('expected HTTP 404')
            except urllib.error.HTTPError as e:
                assert e.code == 404
        finally:
            server.shutdown()

    def test_busy_port_returns_none(self):
        state = HealthState('testd')
        first = start_health_server(state, port=_free_port())
        try:
            busy_port = first.server_address[1]
            assert start_health_server(state, port=busy_port) is None
        finally:
            first.shutdown()


def _free_port():
    import socket
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]
