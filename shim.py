"""tool_reference shim behind an API gateway.

One ThreadingHTTPServer listener on 127.0.0.1 by default; set SHIM_BIND to change it:

    harness -> gateway -> shim (SHIM_PORT) -> SHIM_TARGET (https://api.anthropic.com/v1)

In every `tool_result` whose content holds a `tool_reference` block, the shim removes the
annotations the gateway put beside it. A block is removed only when it is exactly
{"type": "text", "text": ...} and its text matches SHIM_STRIP_PATTERN in full. Any other block
standing beside a reference is left in place and logged as `unmatched`. Everything else (other
tool results, other requests, headers, streaming responses) passes through untouched, and a
request with nothing to remove is forwarded byte for byte.

SHIM_MODE=fix   remove the matching blocks (the default: the shim exists to fix).
SHIM_MODE=log   record only: the same records are written, nothing is rewritten.

Env:
    SHIM_MODE, SHIM_BIND, SHIM_PORT, SHIM_TARGET, SHIM_STRIP_PATTERN (required, no default: a
    Python regular expression matched against the whole text of a block), SHIM_LOG (jsonl),
    CAPTURE_DIR (optional: when unset or empty nothing is written to disk).

Nothing secret is written: `authorization`, `x-api-key`, `cookie` and
`proxy-authorization` are stored as "<redacted>", and any token-shaped string found inside a
logged block is replaced too.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

MODE = os.environ.get("SHIM_MODE", "fix")
PORT = int(os.environ["SHIM_PORT"])
BIND = os.environ.get("SHIM_BIND", "127.0.0.1")
# Where the shim forwards. Defaults to the Anthropic API; override it for another compatible upstream.
TARGET = os.environ.get("SHIM_TARGET", "https://api.anthropic.com/v1")
# What the gateway puts beside a tool_reference, matched against the whole text of a block.
# Required and without a default, so the shape is always the operator's.
STRIP_PATTERN = re.compile(os.environ["SHIM_STRIP_PATTERN"])
LOG_PATH = os.environ.get("SHIM_LOG", "shim-log.jsonl")
# Optional. Capture files hold whole conversations; unset or empty means nothing is written.
CAPTURE = os.environ.get("CAPTURE_DIR") or ""

SECRET = {"authorization", "x-api-key", "cookie", "proxy-authorization"}
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
       "trailers", "transfer-encoding", "upgrade", "host", "content-length"}

# sk-ant-..., long opaque bearer-ish blobs
TOKEN_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{12,}|[A-Za-z0-9_\-]{60,})")

_log_lock = threading.Lock()
_counter = 0
_counter_lock = threading.Lock()


def _next() -> int:
    global _counter
    with _counter_lock:
        _counter += 1
        return _counter


def _safe_headers(headers) -> dict:
    return {k: ("<redacted>" if k.lower() in SECRET else v) for k, v in headers}


def _scrub(obj):
    """Replace token-shaped strings anywhere inside a block before it is logged."""
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    if isinstance(obj, str):
        return TOKEN_RE.sub("<redacted-token>", obj)
    return obj


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _log(record: dict) -> None:
    record["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    record["mode"] = MODE
    line = json.dumps(_scrub(record), ensure_ascii=False)
    with _log_lock:
        with open(LOG_PATH, "a") as fh:
            fh.write(line + "\n")


def _iter_tool_results(payload):
    """Yield every tool_result block in a Messages-API request body."""
    for message in payload.get("messages") or []:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                yield block


def _is_reference(block) -> bool:
    return isinstance(block, dict) and block.get("type") == "tool_reference"


def _is_annotation(block) -> bool:
    """A block the gateway added: exactly `type` and `text`, the text matching the pattern in
    full. Any other key (`cache_control` above all) keeps the block, so a prompt-cache breakpoint
    is never dropped silently."""
    return (isinstance(block, dict) and block.keys() == {"type", "text"}
            and block["type"] == "text" and isinstance(block["text"], str)
            and STRIP_PATTERN.fullmatch(block["text"]) is not None)


def _repair(payload, n: int) -> bool:
    """Remove the annotations beside every tool_reference. Returns True if the payload was
    changed, which never happens in log mode."""
    changed = False
    for block in _iter_tool_results(payload):
        content = block.get("content")
        if not isinstance(content, list) or not any(_is_reference(b) for b in content):
            continue
        tid = block.get("tool_use_id")
        removed = [b for b in content if _is_annotation(b)]
        kept = [b for b in content if not _is_annotation(b)]
        unmatched = [b for b in kept if not _is_reference(b)]
        if removed:
            _log({"request": n, "action": "strip", "tool_use_id": tid,
                  "removed": removed, "kept": kept})
            if MODE == "fix":
                block["content"] = kept
                changed = True
        if unmatched:
            # left in place: the API will refuse the request, and this record says why
            _log({"request": n, "action": "unmatched", "tool_use_id": tid, "blocks": unmatched})
    return changed


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _transform(self, body: bytes, n: int) -> bytes:
        if not body:
            return body
        try:
            payload = json.loads(body)
        except ValueError:
            return body
        if isinstance(payload, dict) and _repair(payload, n):
            return json.dumps(payload, ensure_ascii=False).encode()
        return body

    def _broken(self, action: str, n: int, status: int, sent: int, started: float,
                exc: BaseException, phase: str | None = None) -> None:
        """A relay that did not end normally: the upstream connection failed (`transport`,
        phase `connect` before any response or `stream` mid-body) or the client went away
        while the body was streaming (`client-gone`). Always in the log, never only on stdout:
        these are the events a client reports as a retry, and the terminal is not always there."""
        err = f"{type(exc).__name__}: {exc}"
        record = {"request": n, "action": action, "path": self.path,
                  "status": status, "bytes": sent, "error": err, "duration_ms": _ms(started)}
        if phase:
            record["phase"] = phase
        _log(record)
        print(f"[{n:03d}] {self.command} {self.path} -> {action}"
              f"{' (' + phase + ')' if phase else ''} after {sent} bytes: {err}", flush=True)
        self.close_connection = True

    def _relay(self) -> None:
        n = _next()
        started = time.monotonic()
        # None when CAPTURE_DIR is unset: nothing at all is written to disk then.
        tag = os.path.join(CAPTURE, f"{n:03d}") if CAPTURE else None
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        hdrs = list(self.headers.items())

        try:
            body = self._transform(body, n)
        except Exception as exc:  # noqa: BLE001
            _log({"request": n, "action": "transform-error",
                  "error": f"{type(exc).__name__}: {exc}"})

        if tag:
            with open(tag + "-req.json", "w") as fh:
                json.dump({"method": self.command, "path": self.path,
                           "headers": _safe_headers(hdrs)}, fh, indent=2)
            with open(tag + "-req.body", "wb") as fh:
                fh.write(body)

        parts = urlsplit(TARGET)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
        tls = parts.scheme == "https"
        prefix = parts.path.rstrip("/")
        # a gateway may strip the harness's leading `/v1` for /messages but leave it on other
        # paths (e.g. /v1/messages/count_tokens); with a `/v1` target prefix that doubles into
        # `/v1/v1/...` and a 404. Never prepend a prefix the path already carries.
        path = self.path
        if prefix and (path == prefix or path.startswith(prefix + "/")):
            path = path[len(prefix):]

        conn_cls = http.client.HTTPSConnection if tls else http.client.HTTPConnection
        conn = conn_cls(host, port, timeout=600)
        fwd = [(k, v) for k, v in hdrs if k.lower() not in HOP]
        try:
            conn.putrequest(self.command, prefix + path, skip_host=True,
                            skip_accept_encoding=True)
            conn.putheader("host", host if port in (80, 443) else f"{host}:{port}")
            for k, v in fwd:
                conn.putheader(k, v)
            if body:
                conn.putheader("content-length", str(len(body)))
            conn.endheaders()
            if body:
                conn.send(body)
            resp = conn.getresponse()
        except Exception as exc:  # noqa: BLE001
            if tag:
                with open(tag + "-resp.json", "w") as fh:
                    json.dump({"status": 599, "error": f"{type(exc).__name__}: {exc}"},
                              fh, indent=2)
            self._broken("transport", n, 502, 0, started, exc, phase="connect")
            self.send_response(502)
            self.send_header("content-length", "0")
            self.end_headers()
            return

        rh = resp.getheaders()
        if tag:
            with open(tag + "-resp.json", "w") as fh:
                json.dump({"status": resp.status, "headers": _safe_headers(rh)}, fh, indent=2)

        self.send_response(resp.status)
        for k, v in rh:
            if k.lower() in HOP:
                continue
            self.send_header(k, v)
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()

        out = open(tag + "-resp.body", "wb") if tag else None
        sent = 0
        try:
            while True:
                try:
                    chunk = resp.read(4096)
                except Exception as exc:  # noqa: BLE001
                    # the upstream died mid-body; the client is left with a truncated chunked
                    # stream (no terminating chunk), which is what it then reports as a retry
                    self._broken("transport", n, resp.status, sent, started, exc, phase="stream")
                    return
                if not chunk:
                    break
                if out is not None:
                    out.write(chunk)
                    out.flush()
                try:
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    self.wfile.flush()
                except OSError as exc:
                    self._broken("client-gone", n, resp.status, sent, started, exc)
                    return
                sent += len(chunk)
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError as exc:
                self._broken("client-gone", n, resp.status, sent, started, exc)
                return
        finally:
            if out is not None:
                out.close()
            conn.close()

        ms = _ms(started)
        _log({"request": n, "action": "relay", "status": resp.status,
              "path": self.path, "bytes": sent, "duration_ms": ms})
        print(f"[{n:03d}] {self.command} {self.path} -> {resp.status} {ms}ms", flush=True)

    do_POST = _relay
    do_GET = _relay


class _QuietServer(ThreadingHTTPServer):
    """A peer closing an idle keep-alive connection (the gateway restarting, a CLI session being
    rotated) surfaces as ConnectionResetError while waiting for the next request line. No
    request is lost; the stdlib would print a full traceback per connection. Swallow those."""

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


def main() -> None:
    if CAPTURE:
        os.makedirs(CAPTURE, exist_ok=True)
    print(f"shim mode={MODE} {BIND}:{PORT} -> {TARGET} | "
          f"strip={STRIP_PATTERN.pattern!r} | log={LOG_PATH}", flush=True)
    if CAPTURE:
        print(f"capturing to {CAPTURE}", flush=True)
    _QuietServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
