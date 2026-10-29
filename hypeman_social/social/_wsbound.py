"""Size-bounded websocket reading shared by the chat adapters (internal).

Three pieces, kept in one place so a security fix reaches every adapter:

* ``make_bounded_websocket``: a websocket-client ``WebSocket`` subclass whose
  ``frame_buffer.recv_length`` hook refuses a frame, or a fragmented message,
  over the limit before the payload is read.
* ``probe_reader``: proves on in-process socket pairs that the hook really
  works, so an adapter can fail closed when websocket-client changes.
* ``open_upgraded``: connects with redirects disabled and accepts only a real
  101 upgrade (websocket-client 1.9.0 marks a socket connected after a 3xx reply
  when ``redirect_limit`` is 0; 1.9.2 raises).

The stock reader trusts the length in a frame header and allocates for it (and
for every fragment of a message) before any size check can run, and
websocket-client has no per-message limit option, so this leans on its
``frame_buffer`` internals. There is no fallback to the stock reader.
"""

import socket
import struct
from typing import Any, Callable, Optional, Tuple, Type

__all__ = ['make_bounded_websocket', 'probe_reader', 'reader_failure_message', 'open_upgraded']


def make_bounded_websocket(
    websocket: Any,
    too_large: Type[Exception],
    default_limit: int,
    extend: Optional[Callable[[Any], Any]] = None,
) -> Tuple[Any, Optional[str]]:
    """(class, None) for a WebSocket that refuses oversize frames up front, or
    (None, reason) when this websocket-client cannot support it.

    ``websocket`` is the adapter's imported module (None when not installed);
    ``too_large`` is raised on an oversize frame or message; ``default_limit`` is
    used unless the instance is built with ``max_bytes=``. ``extend`` may return
    a subclass of the bounded class (for adapter-specific behavior).
    """
    if websocket is None:
        return None, 'websocket-client is not installed'
    try:
        from websocket._abnf import ABNF, frame_buffer
    except ImportError:
        return None, 'websocket._abnf.frame_buffer cannot be imported'
    for attribute in ('recv_length', 'recv_frame'):
        if not callable(getattr(frame_buffer, attribute, None)):
            return None, f'frame_buffer.{attribute} is missing'

    class _BoundedFrameBuffer(frame_buffer):  # type: ignore[valid-type,misc]
        def __init__(self, *args: Any, limit: int = default_limit, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.limit = limit
            self._message_bytes = 0

        def recv_length(self) -> None:
            super().recv_length()
            header: Any = self.header
            length: Any = self.length
            if header is None or not isinstance(length, int) or isinstance(length, bool):
                raise too_large('frame length is unreadable')  # fail closed
            # header is (fin, rsv1, rsv2, rsv3, opcode, mask, length)
            earlier = self._message_bytes if header[4] == ABNF.OPCODE_CONT else 0
            if length + earlier > self.limit:
                raise too_large('frame over the size limit')

        def recv_frame(self) -> Any:
            frame = super().recv_frame()
            if frame.opcode in (ABNF.OPCODE_TEXT, ABNF.OPCODE_BINARY):
                self._message_bytes = 0 if frame.fin else len(frame.data)
            elif frame.opcode == ABNF.OPCODE_CONT:
                self._message_bytes = 0 if frame.fin else self._message_bytes + len(frame.data)
            return frame

    class _BoundedWebSocket(websocket.WebSocket):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            limit = kwargs.pop('max_bytes', default_limit)
            super().__init__(*args, **kwargs)
            self.frame_buffer = _BoundedFrameBuffer(
                self._recv, bool(kwargs.get('skip_utf8_validation', False)), limit=limit)

    return (extend(_BoundedWebSocket) if extend else _BoundedWebSocket), None


def probe_reader(cls: Any, too_large: Type[Exception]) -> Optional[str]:
    """Prove, on in-process socket pairs, that ``cls`` refuses a frame whose header
    declares more than its limit before the payload arrives (only a few payload
    bytes are ever sent, so a trusting reader times out), refuses a fragmented
    message whose total passes the limit, and still reads an ordinary frame.
    Returns a reason on failure, None on success."""
    def read(raw: bytes, expect: Optional[type]) -> Optional[str]:
        reader, writer = socket.socketpair()
        reader.settimeout(0.5)
        try:
            ws = cls(max_bytes=100)
            ws.sock = reader
            ws.connected = True
            writer.sendall(raw)
            try:
                got = ws.recv()
            except Exception as exc:
                return None if expect and isinstance(exc, expect) else type(exc).__name__
            return 'accepted' if expect else ('ok' if got == 'hello' else 'misread')
        finally:
            reader.close()
            writer.close()

    try:
        huge = struct.pack('!BBQ', 0x81, 127, 1 << 40) + b'x' * 8
        split = struct.pack('!BB', 0x01, 60) + b'a' * 60 + struct.pack('!BB', 0x80, 60) + b'b' * 8
        if read(huge, too_large) is not None:
            return 'an oversize frame was not refused before its payload was read'
        if read(split, too_large) is not None:
            return 'a fragmented message over the limit was not refused'
        if read(struct.pack('!BB', 0x81, 5) + b'hello', None) != 'ok':
            return 'an ordinary frame was not read correctly'
    except Exception:
        return 'the compatibility probe itself failed'
    return None


def reader_failure_message(label: str, websocket: Any, reason: str) -> str:
    """The one clear error an adapter reports when the bounded reader is unusable."""
    version = getattr(websocket, '__version__', 'unknown') if websocket else 'not installed'
    return (f'{label} chat disabled: websocket-client {version} cannot provide the '
            f'size-bounded reader this adapter requires ({reason}). Install a tested '
            f'version: pip install "websocket-client>=1.6.0,<2"')


def open_upgraded(websocket: Any, url: str, cls: Any,
                  fail: Callable[[str], BaseException], **options: Any) -> Any:
    """Connect with redirects disabled and return the socket only if the handshake
    was a real 101 upgrade; otherwise close it and raise ``fail(message)``.

    redirect_limit=0: websocket-client would otherwise follow a 3xx from the
    handshake to any host, past every URL check made before connecting. The
    status is checked as well because 1.9.0 accepts a 3xx reply at limit 0.
    """
    ws = websocket.create_connection(url, redirect_limit=0, class_=cls, **options)
    try:
        if ws.getstatus() != 101:
            raise fail('handshake was not an upgrade')
        return ws
    except BaseException:
        try:
            ws.close()
        except Exception:
            pass  # the caller gets the original failure
        raise
