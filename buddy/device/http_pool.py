"""Persistent-connection HTTPS client for the Worker.

Why this exists: stock ``urequests`` opens a fresh TCP+TLS connection
per call, paying the ~1-2 s mbedTLS handshake every time. The pager
polls the Worker every 4 s in inbox view and every ~10 s in detail
view (long-poll), so this handshake cost dominates perceived latency
and burns the LiPo. Keep-alive across requests means the first call
pays the handshake and every subsequent one pays only the RTT.

Tradeoff vs urequests: we own the HTTP/1.1 parser, so chunked
transfer-encoding (Cloudflare's default for streamed bodies) and
Connection: keep-alive framing are handled here. Cloudflare edge
serves HTTP/1.1 with keep-alive enabled by default.

Memory: one connection holds ~30 KB during the TLS handshake and
~6 KB steady-state. We ``gc.collect()`` before opening the first
one. Idle connections older than ``_IDLE_TIMEOUT_MS`` are dropped on
next request — Cloudflare's edge keepalive is ~100 s but corporate
WiFi NAT can be more aggressive, and 25 s comfortably brackets both
the inbox refresh cadence (4 s) and notif poll cadence (15 s).

Safety: on any OSError we drop the cached connection and reopen
once. This handles Broken Pipe (peer closed during idle), Reset
(NAT timeout), and TLS-level errors. Two consecutive failures
surface to the caller — typically the user just lost WiFi.

Not handled (intentionally):
- HTTP redirects (the Worker never redirects)
- Cookies (no session state)
- gzip (Cloudflare doesn't compress small responses to keep-alive
  clients with no Accept-Encoding header)
"""

import gc
import json as _json
import socket
import ssl
import time

_IDLE_TIMEOUT_MS = 25000

# Cache of live SSL sockets. Keyed by (host, port). Each entry is
# {"sock": <raw socket>, "ss": <ssl socket>, "last_ms": ticks_ms}.
# Keep both refs so we can close them in the right order.
_conns = {}


def _parse_url(url):
    """Returns (host, port, path). Path includes leading slash and any
    query string. Only https supported — the Worker is always TLS."""
    if not url.startswith("https://"):
        raise ValueError("only https supported: " + url)
    rest = url[8:]
    slash = rest.find("/")
    if slash == -1:
        host_port, path = rest, "/"
    else:
        host_port, path = rest[:slash], rest[slash:]
    if ":" in host_port:
        host, port_s = host_port.rsplit(":", 1)
        port = int(port_s)
    else:
        host, port = host_port, 443
    return host, port, path


def _open(host, port, connect_timeout_s=10):
    """Open a fresh TCP+TLS connection. Caller installs into _conns."""
    gc.collect()
    addr = socket.getaddrinfo(host, port)[0][-1]
    sock = socket.socket()
    try:
        sock.settimeout(connect_timeout_s)
    except Exception:
        pass
    sock.connect(addr)
    # Disable Nagle so small JSON payloads ship immediately. Pager
    # bodies are typically <500 B, well under MSS; Nagle adds ~40 ms
    # of latency for no gain.
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass
    ss = ssl.wrap_socket(sock, server_hostname=host)
    return sock, ss


def _get(host, port):
    """Return a live ssl socket, opening one if needed. Drops stale
    cached entries silently."""
    key = (host, port)
    e = _conns.get(key)
    if e is not None:
        if time.ticks_diff(time.ticks_ms(), e["last_ms"]) < _IDLE_TIMEOUT_MS:
            return e["ss"], e["sock"]
        _drop_key(key)
    sock, ss = _open(host, port)
    _conns[key] = {"sock": sock, "ss": ss, "last_ms": time.ticks_ms()}
    return ss, sock


def _drop_key(key):
    e = _conns.pop(key, None)
    if not e:
        return
    try:
        e["ss"].close()
    except Exception:
        pass
    try:
        e["sock"].close()
    except Exception:
        pass


def drop_all():
    """Close every cached connection. Call after a WiFi reconnect so
    we don't reuse a socket whose TCP underlay is dead. Cheap — empty
    cache is the steady state on a fresh boot."""
    for k in list(_conns.keys()):
        _drop_key(k)


def _build_req(method, path, host, body, headers):
    """Build the raw HTTP/1.1 request bytes. Always sends
    Connection: keep-alive (the whole point of this module)."""
    lines = ["{} {} HTTP/1.1".format(method, path)]
    h = {
        "Host": host,
        "Connection": "keep-alive",
        "User-Agent": "m5-cardputer",
        "Accept": "*/*",
    }
    if headers:
        h.update(headers)
    h["Content-Length"] = str(len(body) if body else 0)
    for k, v in h.items():
        lines.append("{}: {}".format(k, v))
    out = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
    if body:
        out += body
    return out


def _read_more(ss, deadline_ms, sz=512):
    """Single read attempt against the ssl socket. Raises OSError if
    the read fails OR if the overall deadline has elapsed before this
    read started. Returns empty bytes on EOF — caller decides if
    that's an error (truncated body) or expected (Connection: close)."""
    if time.ticks_diff(time.ticks_ms(), deadline_ms) >= 0:
        raise OSError("deadline elapsed")
    return ss.read(sz)


def _parse_response(ss, deadline_ms):
    """Read + parse one HTTP/1.1 response. Returns
    (status, headers_lower, body_bytes, keep_alive_bool).

    Handles Content-Length and Transfer-Encoding: chunked. Does NOT
    handle gzip — we don't send Accept-Encoding."""
    buf = bytearray()
    # Headers terminated by CRLF CRLF.
    while True:
        idx = bytes(buf).find(b"\r\n\r\n")
        if idx != -1:
            head = bytes(buf[:idx])
            rest = bytes(buf[idx + 4:])
            break
        chunk = _read_more(ss, deadline_ms)
        if not chunk:
            raise OSError("eof before headers")
        buf += chunk
        if len(buf) > 16384:
            raise OSError("oversize headers")

    lines = head.split(b"\r\n")
    parts = lines[0].split(b" ", 2)
    if len(parts) < 2:
        raise OSError("bad status line")
    try:
        status = int(parts[1])
    except Exception:
        raise OSError("bad status code")

    hdrs = {}
    for hl in lines[1:]:
        c = hl.find(b":")
        if c <= 0:
            continue
        k = hl[:c].decode("ascii", "replace").strip().lower()
        v = hl[c + 1:].decode("ascii", "replace").strip()
        hdrs[k] = v

    te = hdrs.get("transfer-encoding", "").lower()
    cl_s = hdrs.get("content-length")
    keep_alive = hdrs.get("connection", "keep-alive").lower() != "close"

    if te == "chunked":
        body = _read_chunked(ss, rest, deadline_ms)
    elif cl_s is not None:
        try:
            cl = int(cl_s)
        except Exception:
            cl = 0
        body = bytearray(rest)
        while len(body) < cl:
            chunk = _read_more(ss, deadline_ms, min(1024, cl - len(body)))
            if not chunk:
                raise OSError("eof mid-body")
            body += chunk
        body = bytes(body)
    else:
        # No CL, no TE — read until close. Means keep-alive is off
        # regardless of header (HTTP/1.1 RFC).
        body = bytearray(rest)
        while True:
            try:
                chunk = _read_more(ss, deadline_ms)
            except OSError:
                break
            if not chunk:
                break
            body += chunk
        body = bytes(body)
        keep_alive = False

    return status, hdrs, body, keep_alive


def _read_chunked(ss, prefix, deadline_ms):
    """Decode RFC-7230 chunked transfer-encoding. Returns the
    concatenated decoded body bytes."""
    buf = bytearray(prefix)
    out = bytearray()
    while True:
        eol = bytes(buf).find(b"\r\n")
        while eol == -1:
            chunk = _read_more(ss, deadline_ms)
            if not chunk:
                raise OSError("eof in chunk header")
            buf += chunk
            eol = bytes(buf).find(b"\r\n")
        size_bytes = bytes(buf[:eol]).split(b";")[0].strip()
        try:
            size = int(size_bytes, 16)
        except Exception:
            raise OSError("bad chunk size")
        # consume size line + CRLF
        buf = buf[eol + 2:]
        if size == 0:
            # optional trailer headers terminated by another CRLF
            while bytes(buf).find(b"\r\n") == -1:
                chunk = _read_more(ss, deadline_ms)
                if not chunk:
                    break
                buf += chunk
            return bytes(out)
        need = size + 2  # payload + trailing CRLF
        while len(buf) < need:
            chunk = _read_more(ss, deadline_ms, min(1024, need - len(buf)))
            if not chunk:
                raise OSError("eof in chunk body")
            buf += chunk
        out += buf[:size]
        buf = buf[size + 2:]


def request(method, url, body=None, headers=None, timeout_ms=15000):
    """Make an HTTPS request through the pool. Retries once on a
    stale/broken socket. Returns (status, body_bytes, headers_lower).

    ``body`` may be ``None`` / ``bytes`` / ``str`` / ``dict``.
    Dicts are JSON-encoded automatically.
    """
    host, port, path = _parse_url(url)
    if isinstance(body, dict):
        body = _json.dumps(body).encode("utf-8")
    elif isinstance(body, str):
        body = body.encode("utf-8")

    req = _build_req(method.upper(), path, host, body, headers)
    deadline = time.ticks_add(time.ticks_ms(), timeout_ms)

    last_err = None
    for attempt in (0, 1):
        try:
            ss, sock = _get(host, port)
            # Refresh the underlying socket timeout so a long-poll
            # call gets the whole budget. settimeout on the raw
            # socket applies to subsequent SSL reads.
            try:
                sock.settimeout(max(1.0, timeout_ms / 1000.0))
            except Exception:
                pass
            ss.write(req)
            status, hdrs, body_bytes, keep_alive = _parse_response(ss, deadline)
            if keep_alive:
                _conns[(host, port)]["last_ms"] = time.ticks_ms()
            else:
                _drop_key((host, port))
            return status, body_bytes, hdrs
        except OSError as e:
            last_err = e
            _drop_key((host, port))
            if attempt == 1:
                break
    raise last_err if last_err else OSError("request failed")


def request_json(method, url, body=None, headers=None, timeout_ms=15000):
    """Like ``request()`` but parses JSON. Returns (status, value)
    where value is the parsed dict/list on success, the raw decoded
    text on non-JSON 200, or ``{}`` on empty body. Network errors
    become (0, "transport: <msg>") — mirrors the old _http_json
    contract so callers don't need to add try/except for transport
    failures."""
    h = dict(headers or {})
    has_ct = False
    for k in h:
        if k.lower() == "content-type":
            has_ct = True
            break
    if body is not None and not has_ct:
        h["content-type"] = "application/json"
    try:
        status, body_bytes, _ = request(
            method, url, body=body, headers=h, timeout_ms=timeout_ms,
        )
    except OSError as e:
        return 0, "transport: {}".format(e)
    except Exception as e:
        return 0, "transport: {}".format(e)
    if not body_bytes:
        return status, {}
    try:
        text = body_bytes.decode("utf-8", "replace")
    except Exception:
        return status, ""
    if not text:
        return status, {}
    try:
        return status, _json.loads(text)
    except Exception:
        return status, text


def warm(url):
    """Open the pool connection eagerly so the next request pays only
    the RTT. Best called once after WiFi connect, before the first
    user-visible action. Returns True if the handshake succeeded."""
    try:
        host, port, _ = _parse_url(url)
        _get(host, port)
        return True
    except Exception:
        return False
