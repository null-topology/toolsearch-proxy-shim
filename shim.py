"""Two-sided tool_reference shim around an API gateway.

One process, two ThreadingHTTPServer listeners on 127.0.0.1 by default; set SHIM_BIND to change it:

    harness -> IN (SHIM_IN_PORT)  -> IN_TARGET  (the gateway route naming OUT as upstream)
    gateway -> OUT (SHIM_OUT_PORT) -> OUT_TARGET (https://api.anthropic.com/v1)

IN  remembers, per `tool_use_id`, the `tool_reference` blocks the harness put into a
    `tool_result.content` array, and forwards the request byte for byte.
OUT repairs what the gateway did to those blocks:
      * content array still contains tool_reference blocks -> keep ONLY those, drop the rest;
      * content has no tool_reference but the id is remembered -> restore the remembered blocks
        as the whole content.
    Everything else (other requests, headers, streaming responses) passes through untouched.

SHIM_MODE=fix   apply the two rules above (the default: the shim exists to fix).
SHIM_MODE=log   record only: nothing is rewritten, but every tool_result that the IN side
                remembered is logged exactly as the gateway sent it (this is the diagnostic).

Env:
    SHIM_MODE, SHIM_IN_PORT, SHIM_OUT_PORT, IN_TARGET, OUT_TARGET, SHIM_AUTH_TOKEN,
    SHIM_LOG (jsonl), CAPTURE_DIR (optional: when unset or empty nothing is written to disk),
    REPLAY_PROMPT, REPLAY_MARKER (required, no default: how a gateway's own hidden request is
    recognised; pass an empty value to switch that half of the test off).

Cache accounting. Every OUT response is parsed for the API's `usage` (from the SSE
`message_start`/`message_delta` events, or the JSON body of a non-streamed reply) and logged
as an `action: "usage"` record: input_tokens, cache_creation_input_tokens,
cache_read_input_tokens, output_tokens, plus `origin`: "harness" when the request is the
CLI's own, "gateway-replay" when the gateway issued it by itself (a hidden request it
makes on its own behalf, recognised by REPLAY_PROMPT / REPLAY_MARKER in the last user
message), or "count_tokens". Running totals per origin ride along in the same record, and the stdout line
carries the same numbers. The accounting reads an in-memory buffer, so it works whether or not
CAPTURE_DIR is set.

Nothing secret is written: `authorization`, `x-api-key`, `cookie` and
`proxy-authorization` are stored as "<redacted>", and any token-shaped string found inside a
logged block is replaced too.
"""
from __future__ import annotations

import gzip
import hmac
import http.client
import json
import zlib
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

MODE = os.environ.get("SHIM_MODE", "fix")
IN_PORT = int(os.environ["SHIM_IN_PORT"])
OUT_PORT = int(os.environ["SHIM_OUT_PORT"])
BIND = os.environ.get("SHIM_BIND", "127.0.0.1")
IN_TARGET = os.environ["IN_TARGET"]
# Where OUT forwards. Defaults to the Anthropic API; override it for another compatible upstream.
OUT_TARGET = os.environ.get("OUT_TARGET", "https://api.anthropic.com/v1")
LOG_PATH = os.environ.get("SHIM_LOG", "shim-log.jsonl")
# Optional. Capture files hold whole conversations; unset or empty means nothing is written.
CAPTURE = os.environ.get("CAPTURE_DIR") or ""
# How a gateway's own hidden request is recognised: it opens the last user message with
# REPLAY_PROMPT, or carries REPLAY_MARKER somewhere in it. Either signal is enough. Required and
# without a default, so the wording is always the operator's; an empty value disables that half.
REPLAY_PROMPT = os.environ["REPLAY_PROMPT"]
REPLAY_MARKER = os.environ["REPLAY_MARKER"]
AUTH_TOKEN = os.environ.get("SHIM_AUTH_TOKEN", "")

AUTH_HEADER = "X-Shim-Token"
SECRET = {"authorization", "x-api-key", "cookie", "proxy-authorization"}
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
       "trailers", "transfer-encoding", "upgrade", "host", "content-length"}

# sk-ant-..., long opaque bearer-ish blobs
TOKEN_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{12,}|[A-Za-z0-9_\-]{60,})")

REFS: dict[str, list] = {}
_refs_lock = threading.Lock()
_log_lock = threading.Lock()
_counters = {"in": 0, "out": 0}
_counter_lock = threading.Lock()


def _next(side: str) -> int:
    with _counter_lock:
        _counters[side] += 1
        return _counters[side]


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


def _references(content) -> list:
    if not isinstance(content, list):
        return []
    return [b for b in content
            if isinstance(b, dict) and b.get("type") == "tool_reference"]


def _remember(payload, n: int) -> None:
    """IN side: record tool_use_id -> the tool_reference blocks the harness sent."""
    for block in _iter_tool_results(payload):
        refs = _references(block.get("content"))
        if not refs:
            continue
        tid = block.get("tool_use_id")
        if not tid:
            continue
        with _refs_lock:
            known = REFS.get(tid)
            REFS[tid] = refs
        if known is None:
            _log({"side": "in", "request": n, "action": "remember",
                  "tool_use_id": tid, "blocks": refs})


def _repair(payload, n: int) -> bool:
    """OUT side: apply the two rules. Returns True if the payload was changed."""
    changed = False
    for block in _iter_tool_results(payload):
        tid = block.get("tool_use_id")
        content = block.get("content")
        refs = _references(content)
        if refs:
            if len(refs) != len(content):
                removed = [b for b in content if b not in refs]
                _log({"side": "out", "request": n, "action": "strip",
                      "tool_use_id": tid, "removed": removed, "kept": refs})
                if MODE == "fix":
                    block["content"] = refs
                    changed = True
            continue
        with _refs_lock:
            remembered = REFS.get(tid)
        if remembered is None:
            continue
        # the gateway erased the reference entirely: what is standing in its place?
        _log({"side": "out", "request": n, "action": "restore",
              "tool_use_id": tid, "erased_content": content, "restored": remembered})
        if MODE == "fix":
            block["content"] = remembered
            changed = True
    return changed


USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
              "output_tokens")
RESP_BUFFER_CAP = 8 * 1024 * 1024  # a 64k-token reply is well under 1 MB; never grow unbounded

TOTALS: dict[str, dict] = {}
_totals_lock = threading.Lock()


def _origin(payload, path: str) -> str:
    """Who issued this OUT request: the CLI, or the gateway on its own behalf."""
    if path.rstrip("/").endswith("/count_tokens"):
        return "count_tokens"
    messages = payload.get("messages") or []
    last = messages[-1] if messages else None
    if isinstance(last, dict) and last.get("role") == "user":
        content = last.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list) and content:
            tail = content[-1]
            if isinstance(tail, dict) and tail.get("type") == "text":
                text = tail.get("text") or ""
        # An empty variable switches its signal off; without the guard it would match every
        # request, since "".startswith / "" in text are always true.
        if ((REPLAY_PROMPT and text.lstrip().startswith(REPLAY_PROMPT))
                or (REPLAY_MARKER and REPLAY_MARKER in text)):
            return "gateway-replay"
    return "harness"


def _decode_body(raw: bytes, headers) -> bytes:
    enc = ""
    for k, v in headers:
        if k.lower() == "content-encoding":
            enc = v.lower()
    if enc == "gzip":
        return gzip.decompress(raw)
    if enc == "deflate":
        return zlib.decompress(raw)
    return raw


def _usage_from_response(raw: bytes, headers) -> dict | None:
    """Pull the API's usage out of a response body: SSE (message_start carries the input
    side, message_delta the output side and, on newer servers, cumulative totals) or a plain
    JSON reply. None when there is nothing to read (errors, unknown shapes)."""
    body = _decode_body(raw, headers)
    usage: dict = {}
    if body.lstrip().startswith(b"{"):
        try:
            doc = json.loads(body)
        except ValueError:
            return None
        found = doc.get("usage") if isinstance(doc, dict) else None
        if isinstance(found, dict):
            usage.update(found)
    else:
        for line in body.split(b"\n"):
            if not line.startswith(b"data: "):
                continue
            try:
                ev = json.loads(line[6:])
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            kind = ev.get("type")
            if kind == "message_start":
                found = (ev.get("message") or {}).get("usage")
                if isinstance(found, dict):
                    usage.update(found)
            elif kind == "message_delta":
                found = ev.get("usage")
                if isinstance(found, dict):
                    usage.update({k: v for k, v in found.items() if v is not None})
    if not usage:
        return None
    return {k: int(usage.get(k) or 0) for k in USAGE_KEYS}


def _account(origin: str, usage: dict) -> dict:
    with _totals_lock:
        bucket = TOTALS.setdefault(origin, {k: 0 for k in USAGE_KEYS} | {"requests": 0})
        bucket["requests"] += 1
        for k in USAGE_KEYS:
            bucket[k] += usage[k]
        return {name: dict(b) for name, b in TOTALS.items()}


def _usage_line(usage: dict) -> str:
    return (f"in={usage['input_tokens']} cache_w={usage['cache_creation_input_tokens']} "
            f"cache_r={usage['cache_read_input_tokens']} out={usage['output_tokens']}")


def _inspect_out(payload, n: int) -> None:
    """log mode: report every tool_result whose id the IN side remembered, verbatim."""
    for block in _iter_tool_results(payload):
        tid = block.get("tool_use_id")
        with _refs_lock:
            known = tid in REFS
        if known:
            _log({"side": "out", "request": n, "action": "observe",
                  "tool_use_id": tid, "content_as_the_gateway_sent_it": block.get("content")})


class _Base(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    side = "in"
    target = ""

    def log_message(self, fmt, *args):
        pass

    def _transform(self, body: bytes, n: int) -> bytes:
        raise NotImplementedError

    def _broken(self, action: str, n: int, status: int, sent: int, started: float,
                exc: BaseException, phase: str | None = None) -> None:
        """A relay that did not end normally: the upstream connection failed (`transport`,
        phase `connect` before any response or `stream` mid-body) or the client went away
        while the body was streaming (`client-gone`). Always in the log, never only on stdout:
        these are the events a client reports as a retry, and the terminal is not always there."""
        err = f"{type(exc).__name__}: {exc}"
        record = {"side": self.side, "request": n, "action": action, "path": self.path,
                  "status": status, "bytes": sent, "error": err, "duration_ms": _ms(started)}
        if phase:
            record["phase"] = phase
        _log(record)
        print(f"[{self.side} {n:03d}] {self.command} {self.path} -> {action}"
              f"{' (' + phase + ')' if phase else ''} after {sent} bytes: {err}", flush=True)
        self.close_connection = True

    def _relay(self) -> None:
        n = _next(self.side)
        started = time.monotonic()
        if self.side == "in" and AUTH_TOKEN:
            token = self.headers.get(AUTH_HEADER, "")
            if not hmac.compare_digest(token, AUTH_TOKEN):
                body = b'{"error": "unauthorized"}'
                _log({"side": "in", "request": n, "action": "reject", "status": 401,
                      "path": self.path})
                self.close_connection = True
                self.send_response(401)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        # None when CAPTURE_DIR is unset: nothing at all is written to disk then.
        tag = os.path.join(CAPTURE, self.side, f"{n:03d}") if CAPTURE else None
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        hdrs = list(self.headers.items())
        if self.side == "in" and AUTH_TOKEN:
            hdrs = [(k, v) for k, v in hdrs if k.lower() != AUTH_HEADER.lower()]

        try:
            body = self._transform(body, n)
        except Exception as exc:  # noqa: BLE001
            _log({"side": self.side, "request": n, "action": "transform-error",
                  "error": f"{type(exc).__name__}: {exc}"})

        if tag:
            with open(tag + "-req.json", "w") as fh:
                json.dump({"method": self.command, "path": self.path,
                           "headers": _safe_headers(hdrs)}, fh, indent=2)
            with open(tag + "-req.body", "wb") as fh:
                fh.write(body)

        parts = urlsplit(self.target)
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
        buf = bytearray()
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
                if len(buf) < RESP_BUFFER_CAP:
                    buf.extend(chunk)
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
        line = f"[{self.side} {n:03d}] {self.command} {self.path} -> {resp.status} {ms}ms"
        if self.side == "out":
            line += self._account_usage(bytes(buf), rh, n, resp.status, ms)
        else:
            _log({"side": "in", "request": n, "action": "relay", "status": resp.status,
                  "path": self.path, "bytes": sent, "duration_ms": ms})
        print(line, flush=True)

    def _account_usage(self, raw: bytes, headers, n: int, status: int, ms: int) -> str:
        origin = getattr(self, "origin", "unknown")
        try:
            usage = _usage_from_response(raw, headers)
        except Exception as exc:  # noqa: BLE001
            _log({"side": "out", "request": n, "action": "usage-error", "origin": origin,
                  "status": status, "duration_ms": ms, "error": f"{type(exc).__name__}: {exc}"})
            return f" origin={origin} usage=unreadable"
        if usage is None:
            _log({"side": "out", "request": n, "action": "usage", "origin": origin,
                  "status": status, "path": self.path, "duration_ms": ms, "usage": None})
            return f" origin={origin} usage=none"
        totals = _account(origin, usage)
        _log({"side": "out", "request": n, "action": "usage", "origin": origin,
              "status": status, "path": self.path, "duration_ms": ms, "usage": usage,
              "totals": totals})
        mine = totals[origin]
        return (f" origin={origin} {_usage_line(usage)} | total[{origin}] "
                f"n={mine['requests']} {_usage_line(mine)}")

    do_POST = _relay
    do_GET = _relay


class InHandler(_Base):
    side = "in"
    target = IN_TARGET

    def _transform(self, body: bytes, n: int) -> bytes:
        if body:
            try:
                payload = json.loads(body)
            except ValueError:
                return body
            if isinstance(payload, dict):
                _remember(payload, n)
        return body  # IN never rewrites


class OutHandler(_Base):
    side = "out"
    target = OUT_TARGET

    def _transform(self, body: bytes, n: int) -> bytes:
        if not body:
            return body
        try:
            payload = json.loads(body)
        except ValueError:
            return body
        if not isinstance(payload, dict):
            return body
        self.origin = _origin(payload, self.path)
        if MODE == "fix":
            if _repair(payload, n):
                return json.dumps(payload, ensure_ascii=False).encode()
            return body
        _inspect_out(payload, n)
        _repair(payload, n)  # logs only; MODE != "fix" so nothing is written back
        return body


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


def _serve(port: int, handler) -> None:
    _QuietServer((BIND, port), handler).serve_forever()


def main() -> None:
    if CAPTURE:
        os.makedirs(os.path.join(CAPTURE, "in"), exist_ok=True)
        os.makedirs(os.path.join(CAPTURE, "out"), exist_ok=True)
    print(f"shim mode={MODE} IN {BIND}:{IN_PORT} -> {IN_TARGET} | "
          f"OUT {BIND}:{OUT_PORT} -> {OUT_TARGET} | log={LOG_PATH} | "
          f"IN auth: {'on' if AUTH_TOKEN else 'off'}", flush=True)
    if CAPTURE:
        print(f"capturing to {CAPTURE}", flush=True)
    threading.Thread(target=_serve, args=(OUT_PORT, OutHandler), daemon=True).start()
    _serve(IN_PORT, InHandler)


if __name__ == "__main__":
    main()
