# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The shared size-bounded websocket reader, its probe and the upgrade check."""

import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

websocket = pytest.importorskip('websocket')
from websocket._abnf import frame_buffer  # noqa: E402  (after the importorskip guard)

from hypeman_social.social import _wsbound  # noqa: E402

LIMIT = 100


class TooBig(Exception):
    pass


def frame(opcode, payload, fin=True):
    first = (0x80 if fin else 0) | opcode
    n = len(payload)
    if n < 126:
        head = struct.pack('!BB', first, n)
    elif n < 65536:
        head = struct.pack('!BBH', first, 126, n)
    else:
        head = struct.pack('!BBQ', first, 127, n)
    return head + payload


@pytest.fixture
def bounded():
    cls, reason = _wsbound.make_bounded_websocket(websocket, TooBig, LIMIT)
    assert reason is None and cls is not None
    return cls


@pytest.fixture
def pair(bounded):
    made = []

    def build(**options):
        near, far = socket.socketpair()
        near.settimeout(1)
        ws = bounded(**options)
        ws.sock = near
        ws.connected = True
        made.append((near, far))
        return ws, far

    yield build
    for near, far in made:
        near.close()
        far.close()


def test_ordinary_frame_is_read(pair):
    ws, peer = pair()
    peer.sendall(frame(0x1, b'hello'))
    assert ws.recv() == 'hello'


def test_frame_at_the_limit_is_read_and_one_over_is_refused(pair):
    ws, peer = pair()
    peer.sendall(frame(0x1, b'a' * LIMIT))
    assert ws.recv() == 'a' * LIMIT
    peer.sendall(frame(0x1, b'a' * (LIMIT + 1)))
    with pytest.raises(TooBig):
        ws.recv()


def test_oversize_header_is_refused_before_the_payload_arrives(pair):
    ws, peer = pair()
    # Only eight payload bytes follow a header declaring 1 TiB: a trusting reader
    # would block until the socket timeout instead of raising.
    peer.sendall(struct.pack('!BBQ', 0x81, 127, 1 << 40) + b'x' * 8)
    with pytest.raises(TooBig):
        ws.recv()


def test_cumulatively_oversize_message_is_refused_on_the_fragment_that_crosses(pair):
    ws, peer = pair()
    peer.sendall(frame(0x1, b'a' * 60, fin=False) + frame(0x0, b'b' * 60, fin=True))
    with pytest.raises(TooBig):
        ws.recv()


def test_fragments_within_the_limit_reassemble_and_the_count_resets(pair):
    ws, peer = pair()
    peer.sendall(frame(0x1, b'a' * 60, fin=False) + frame(0x0, b'b' * 40, fin=True))
    assert ws.recv() == 'a' * 60 + 'b' * 40
    # A new message starts from zero, not from the previous total.
    peer.sendall(frame(0x1, b'c' * 60, fin=False) + frame(0x0, b'd' * 40, fin=True))
    assert ws.recv() == 'c' * 60 + 'd' * 40


def test_per_instance_limit_overrides_the_default(pair):
    ws, peer = pair(max_bytes=10)
    peer.sendall(frame(0x1, b'a' * 11))
    with pytest.raises(TooBig):
        ws.recv()


def test_stock_reader_is_not_bounded(pair):
    near, far = socket.socketpair()
    try:
        near.settimeout(0.3)
        ws = websocket.WebSocket()
        ws.sock = near
        ws.connected = True
        far.sendall(struct.pack('!BBQ', 0x81, 127, 1 << 40) + b'x' * 8)
        with pytest.raises(Exception) as caught:
            ws.recv()
        assert not isinstance(caught.value, TooBig)
    finally:
        near.close()
        far.close()


def test_extend_returns_a_subclass_that_is_still_bounded(pair):
    def extend(base):
        class Extended(base):
            marker = True
        return Extended

    cls, reason = _wsbound.make_bounded_websocket(websocket, TooBig, LIMIT, extend=extend)
    assert reason is None and cls.marker is True
    assert _wsbound.probe_reader(cls, TooBig) is None


# -- the probe and the fail-closed rules ------------------------------------

def test_probe_accepts_the_bounded_reader(bounded):
    assert _wsbound.probe_reader(bounded, TooBig) is None


def test_probe_rejects_the_stock_reader():
    assert _wsbound.probe_reader(websocket.WebSocket, TooBig) is not None


def test_probe_rejects_a_reader_that_ignores_the_running_total(bounded):
    # Per-frame check intact, but the bytes of earlier fragments are never counted.
    class Forgetful(bounded):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            buffer_class = type(self.frame_buffer)

            class NoTotal(buffer_class):
                def recv_frame(self):
                    return frame_buffer.recv_frame(self)

            self.frame_buffer = NoTotal(
                self._recv, False, limit=kwargs.get('max_bytes', LIMIT))

    assert _wsbound.probe_reader(Forgetful, TooBig) == (
        'a fragmented message over the limit was not refused')


def test_missing_hook_fails_closed(monkeypatch):
    monkeypatch.delattr(frame_buffer, 'recv_length')
    cls, reason = _wsbound.make_bounded_websocket(websocket, TooBig, LIMIT)
    assert cls is None and 'recv_length' in reason


def test_missing_recv_frame_fails_closed(monkeypatch):
    monkeypatch.delattr(frame_buffer, 'recv_frame')
    cls, reason = _wsbound.make_bounded_websocket(websocket, TooBig, LIMIT)
    assert cls is None and 'recv_frame' in reason


def test_websocket_client_absent_fails_closed():
    cls, reason = _wsbound.make_bounded_websocket(None, TooBig, LIMIT)
    assert cls is None and 'not installed' in reason


def test_renamed_hook_that_no_longer_reads_the_length_fails_the_probe(monkeypatch):
    monkeypatch.setattr(frame_buffer, 'recv_length', lambda self: None)
    cls, reason = _wsbound.make_bounded_websocket(websocket, TooBig, LIMIT)
    assert cls is not None and reason is None
    assert _wsbound.probe_reader(cls, TooBig) is not None


def test_unreadable_length_is_refused_even_for_an_ordinary_frame(monkeypatch, pair):
    original = frame_buffer.recv_length

    def odd(self):
        original(self)
        self.length = '5'

    monkeypatch.setattr(frame_buffer, 'recv_length', odd)
    cls, _ = _wsbound.make_bounded_websocket(websocket, TooBig, LIMIT)
    assert _wsbound.probe_reader(cls, TooBig) is not None
    near, far = socket.socketpair()
    try:
        near.settimeout(0.5)
        ws = cls()
        ws.sock = near
        ws.connected = True
        far.sendall(frame(0x1, b'hello'))
        with pytest.raises(TooBig):
            ws.recv_data_frame(True)
    finally:
        near.close()
        far.close()


def test_failure_message_names_the_installed_version_and_the_pin():
    text = _wsbound.reader_failure_message('Example', websocket, 'why')
    assert websocket.__version__ in text and 'why' in text
    assert 'websocket-client>=1.6.0,<2' in text and text.startswith('Example chat disabled')
    assert 'not installed' in _wsbound.reader_failure_message('Example', None, 'why')


# -- the upgrade check ----------------------------------------------------------

class StubConn:
    def __init__(self, status):
        self.status, self.closed = status, False

    def getstatus(self):
        if isinstance(self.status, Exception):
            raise self.status
        return self.status

    def close(self):
        self.closed = True


class StubModule:
    def __init__(self, conn):
        self.conn, self.calls = conn, []

    def create_connection(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.conn


def test_a_101_is_handed_over_open_with_redirects_disabled():
    conn = StubConn(101)
    module = StubModule(conn)
    got = _wsbound.open_upgraded(module, 'wss://example.invalid/', 'CLS', OSError, timeout=3)
    assert got is conn and not conn.closed
    assert module.calls == [('wss://example.invalid/',
                             {'redirect_limit': 0, 'class_': 'CLS', 'timeout': 3})]


@pytest.mark.parametrize('status', [301, 302, 307, 200, 400])
def test_a_non_101_status_is_closed_and_rejected(status):
    conn = StubConn(status)
    with pytest.raises(OSError, match='upgrade'):
        _wsbound.open_upgraded(StubModule(conn), 'wss://example.invalid/', None, OSError)
    assert conn.closed


def test_a_failing_status_read_still_closes():
    conn = StubConn(RuntimeError('boom'))
    with pytest.raises(RuntimeError):
        _wsbound.open_upgraded(StubModule(conn), 'wss://example.invalid/', None, OSError)
    assert conn.closed


def test_a_failing_close_does_not_mask_the_original_error():
    class BadClose(StubConn):
        def close(self):
            raise ValueError('close failed')

    with pytest.raises(OSError, match='upgrade'):
        _wsbound.open_upgraded(StubModule(BadClose(302)), 'wss://example.invalid/', None, OSError)


def test_a_real_3xx_handshake_is_rejected_whichever_websocket_client_is_installed():
    """1.9.0 returns a connected socket after the 3xx; 1.9.2 raises. Either way
    the caller must not receive a usable connection."""
    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header('Location', 'ws://127.0.0.1:9/')
            self.send_header('Content-Length', '0')
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(('127.0.0.1', 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cls, _ = _wsbound.make_bounded_websocket(websocket, TooBig, LIMIT)
        with pytest.raises(Exception):
            _wsbound.open_upgraded(
                websocket, f'ws://127.0.0.1:{server.server_address[1]}/', cls,
                OSError, timeout=2)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
