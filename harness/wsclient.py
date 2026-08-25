"""Minimal, dependency-free WebSocket *client* (RFC 6455) — stdlib only.

Collie's core ships zero third-party deps (see pyproject `dependencies = []`), and we already
hand-roll the HTTP *server* in webapp.py, so `collie web --remote` hand-rolls the WS *client* too
rather than pulling in `websockets`/`websocket-client`. The client side is the easy half of 6455:
we only need to *speak* to a Cloudflare Worker relay (wss://), not accept connections.

Scope kept deliberately small — exactly what harness/remote.py needs:
  - ws:// and wss:// (TLS via stdlib ssl)
  - the Upgrade handshake + Sec-WebSocket-Accept validation
  - client->server frames are always masked (RFC 6455 §5.3); server->client are not
  - text / binary data frames, fragmentation (continuation frames) reassembled
  - control frames: auto-PONG on PING, handle CLOSE, expose send_ping()
  - thread-safe sends (one thread reads via recv_message(), another may send)

Not implemented (unneeded here): permessage-deflate, the server role, subprotocol negotiation.
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
import time              # keepalive stamps last_pong = time.time() on every PONG (recv_message)
import urllib.parse

# RFC 6455 §1.3 — the magic GUID concatenated with the client key to derive the accept hash.
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Relay traffic is JSON/control metadata plus bounded HTTP chunks. A peer that advertises a giant
# 64-bit frame must be rejected before allocation, and many small fragments must not bypass the
# same bound. Values can be tuned downward for constrained machines, never above these hard caps.
MAX_FRAME_BYTES = max(1024, min(8 * 1024 * 1024, int(os.environ.get(
    "COLLIE_WS_MAX_FRAME", str(2 * 1024 * 1024)))))
MAX_MESSAGE_BYTES = max(MAX_FRAME_BYTES, min(16 * 1024 * 1024, int(os.environ.get(
    "COLLIE_WS_MAX_MESSAGE", str(4 * 1024 * 1024)))))

# opcodes (RFC 6455 §5.2)
OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(Exception):
    pass


class WebSocketClosed(WebSocketError):
    """Raised by recv_message() when the peer closed (or the socket died). `code`/`reason` set
    when a proper CLOSE frame was received; both None on an abrupt transport drop."""
    def __init__(self, code=None, reason=None):
        self.code = code
        self.reason = reason
        super().__init__("websocket closed (code=%s reason=%r)" % (code, reason))


class WebSocketClient:
    def __init__(self, sock: socket.socket, reader):
        # When the far end last answered a ping. A socket can stay writable long after the
        # other side stopped listening, so this is the only evidence anyone is still there.
        self.last_pong = 0.0
        self._sock = sock
        self._reader = reader            # buffered BufferedReader over the (TLS) socket
        self._send_lock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------------ connect
    @classmethod
    def connect(cls, url: str, headers: dict | None = None, timeout: float = 20.0) -> "WebSocketClient":
        """Open a WebSocket to `url` (ws:// or wss://). `headers` are extra request headers
        (e.g. auth). Raises WebSocketError on a failed handshake."""
        u = urllib.parse.urlsplit(url)
        secure = u.scheme == "wss"
        if u.scheme not in ("ws", "wss"):
            raise WebSocketError("unsupported scheme: %r" % u.scheme)
        host = u.hostname or ""
        port = u.port or (443 if secure else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query

        raw = socket.create_connection((host, port), timeout=timeout)
        try:
            if secure:
                ctx = ssl.create_default_context()
                sock = ctx.wrap_socket(raw, server_hostname=host)
            else:
                sock = raw
        except Exception:
            raw.close()
            raise

        # ---- handshake ----
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = [
            "GET %s HTTP/1.1" % path,
            "Host: %s%s" % (host, "" if port in (80, 443) else ":%d" % port),
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: %s" % key,
            "Sec-WebSocket-Version: 13",
        ]
        for k, v in (headers or {}).items():
            req.append("%s: %s" % (k, v))
        req.append("")
        req.append("")
        try:
            sock.sendall("\r\n".join(req).encode("ascii"))
            reader = sock.makefile("rb")
            status = reader.readline().decode("latin-1", "replace").strip()
            if not status.startswith("HTTP/1.1 101") and not status.startswith("HTTP/1.0 101"):
                # drain a little for a useful error, then fail
                rest = reader.readline().decode("latin-1", "replace").strip()
                raise WebSocketError("handshake failed: %s (%s)" % (status, rest))
            got_accept = None
            while True:
                line = reader.readline()
                if not line or line in (b"\r\n", b"\n"):
                    break
                name, _, val = line.decode("latin-1", "replace").partition(":")
                if name.strip().lower() == "sec-websocket-accept":
                    got_accept = val.strip()
            expect = base64.b64encode(
                hashlib.sha1((key + _GUID).encode("ascii")).digest()
            ).decode("ascii")
            if got_accept != expect:
                raise WebSocketError("bad Sec-WebSocket-Accept: %r != %r" % (got_accept, expect))
        except Exception:
            sock.close()
            raise

        # after a successful upgrade the connection can stay blocking; recv runs in its own thread
        sock.settimeout(None)
        return cls(sock, reader)

    # ------------------------------------------------------------------ framing
    @staticmethod
    def _encode(opcode: int, payload: bytes) -> bytes:
        """Encode one *final* client frame. Clients MUST mask (RFC 6455 §5.3)."""
        if len(payload) > MAX_FRAME_BYTES:
            raise WebSocketError("outgoing websocket frame exceeds %d bytes" % MAX_FRAME_BYTES)
        if opcode in (OP_CLOSE, OP_PING, OP_PONG) and len(payload) > 125:
            raise WebSocketError("websocket control frame exceeds 125 bytes")
        b0 = 0x80 | (opcode & 0x0F)          # FIN=1
        n = len(payload)
        header = bytearray([b0])
        if n < 126:
            header.append(0x80 | n)          # MASK=1 | len
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        return bytes(header) + masked

    def _send_frame(self, opcode: int, payload: bytes):
        if self._closed:
            raise WebSocketClosed()
        data = self._encode(opcode, payload)
        with self._send_lock:
            try:
                self._sock.sendall(data)
            except OSError as e:
                self._closed = True
                raise WebSocketClosed() from e

    def _read_exact(self, n: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < n:
            part = self._reader.read(n - len(chunks))
            if not part:
                raise WebSocketClosed()      # transport EOF mid-frame
            chunks += part
        return bytes(chunks)

    def _read_frame(self, *, allow_masked=False):
        """Read one raw frame → (fin, opcode, payload). Server frames are unmasked."""
        h = self._read_exact(2)
        b0, b1 = h[0], h[1]
        if b0 & 0x70:
            raise WebSocketError("RSV bits set without an negotiated extension")
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        if opcode not in (OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG):
            raise WebSocketError("unknown/reserved opcode 0x%x" % opcode)
        masked = bool(b1 & 0x80)
        if masked and not allow_masked:
            raise WebSocketError("server websocket frame must not be masked")
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
            if length < 126:
                raise WebSocketError("non-minimal websocket length encoding")
        elif length == 127:
            raw_length = self._read_exact(8)
            if raw_length[0] & 0x80:
                raise WebSocketError("invalid websocket 64-bit length")
            length = struct.unpack(">Q", raw_length)[0]
            if length < 65536:
                raise WebSocketError("non-minimal websocket length encoding")
        if opcode in (OP_CLOSE, OP_PING, OP_PONG):
            if not fin or length > 125:
                raise WebSocketError("invalid fragmented/oversized control frame")
        if length > MAX_FRAME_BYTES:
            self._safe_close_frame(1009)
            raise WebSocketError("websocket frame exceeds %d bytes" % MAX_FRAME_BYTES)
        mask = self._read_exact(4) if masked else None
        payload = self._read_exact(length) if length else b""
        if mask:
            payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        return fin, opcode, payload

    # ------------------------------------------------------------------ public API
    def send_text(self, s: str):
        self._send_frame(OP_TEXT, s.encode("utf-8"))

    def send_bytes(self, b: bytes):
        self._send_frame(OP_BINARY, b)

    def send_ping(self, data: bytes = b""):
        self._send_frame(OP_PING, data)

    def recv_message(self):
        """Block until one complete application message arrives; return (kind, data) where kind is
        'text' (str) or 'binary' (bytes). Control frames are handled transparently: PING → auto-PONG,
        PONG → ignored. Raises WebSocketClosed on CLOSE or transport drop.

        Call this from a single dedicated reader thread. sends are safe from other threads."""
        frags = bytearray()
        frag_op = None
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode in (OP_TEXT, OP_BINARY):
                if frag_op is not None:
                    raise WebSocketError("new data frame before fragmented message completed")
                frag_op = opcode
                frags = bytearray(payload)
                if len(frags) > MAX_MESSAGE_BYTES:
                    self._safe_close_frame(1009)
                    raise WebSocketError("websocket message exceeds %d bytes" % MAX_MESSAGE_BYTES)
                if fin:
                    return self._deliver(frag_op, bytes(frags))
            elif opcode == OP_CONT:
                if frag_op is None:
                    raise WebSocketError("continuation frame with no start")
                if len(frags) + len(payload) > MAX_MESSAGE_BYTES:
                    self._safe_close_frame(1009)
                    raise WebSocketError("websocket message exceeds %d bytes" % MAX_MESSAGE_BYTES)
                frags += payload
                if fin:
                    return self._deliver(frag_op, bytes(frags))
            elif opcode == OP_PING:
                self._send_frame(OP_PONG, payload)      # echo per RFC 6455 §5.5.2
            elif opcode == OP_PONG:
                # Remember WHEN, not just that it happened. A socket can stay writable long after the
                # far end has stopped answering — pings keep succeeding, nothing raises, and the
                # caller believes it is connected while no traffic reaches it. The reply is the only
                # evidence anyone is still there.
                self.last_pong = time.time()
            elif opcode == OP_CLOSE:
                code, reason = None, None
                if len(payload) == 1:
                    raise WebSocketError("invalid websocket close payload")
                if len(payload) >= 2:
                    code = struct.unpack(">H", payload[:2])[0]
                    try:
                        reason = payload[2:].decode("utf-8", "strict")
                    except UnicodeDecodeError as exc:
                        self._safe_close_frame(1007)
                        raise WebSocketError("invalid UTF-8 close reason") from exc
                self._safe_close_frame(code)
                raise WebSocketClosed(code, reason)
            else:
                raise WebSocketError("unknown opcode 0x%x" % opcode)

    @staticmethod
    def _deliver(op, data):
        if op == OP_TEXT:
            try:
                return "text", data.decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                raise WebSocketError("invalid UTF-8 text message") from exc
        return "binary", data

    def _safe_close_frame(self, code):
        try:
            payload = struct.pack(">H", code) if code else b""
            self._send_frame(OP_CLOSE, payload)
        except Exception:
            pass

    def close(self, code: int = 1000):
        if self._closed:
            return
        self._safe_close_frame(code)
        self._closed = True
        try:
            self._sock.close()
        except Exception:
            pass


# --------------------------------------------------------------------- self-test (no network)
def _selftest():
    """Round-trip the frame codec without a socket: encode a client frame, then decode it as if
    we were the server (unmask), for a few payload sizes incl. the 126/65536 length boundaries."""
    class _FakeReader:
        def __init__(self, data): self.data = data; self.i = 0
        def read(self, n):
            b = self.data[self.i:self.i + n]; self.i += n; return b

    def roundtrip(op, payload):
        frame = WebSocketClient._encode(op, payload)
        # decode as a server would: MASK bit must be set on a client frame
        assert frame[1] & 0x80, "client frame must set MASK bit"
        c = WebSocketClient.__new__(WebSocketClient)
        c._reader = _FakeReader(frame)
        fin, opcode, out = c._read_frame(allow_masked=True)
        assert fin and opcode == op and out == payload, (fin, opcode, len(out), len(payload))

    for n in (0, 1, 125, 126, 127, 200, 65535, 65536, 70000):
        roundtrip(OP_BINARY, os.urandom(n))
    roundtrip(OP_TEXT, "héllo 世界".encode("utf-8"))
    # accept-hash vector from RFC 6455 §1.3
    key = "dGhlIHNhbXBsZSBub25jZQ=="
    acc = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
    assert acc == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", acc
    print("wsclient selftest OK")


if __name__ == "__main__":
    _selftest()
