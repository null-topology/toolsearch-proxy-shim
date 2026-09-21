"""End-to-end tests for shim.py.

A stub upstream runs in a thread in this process; the shim runs as a real subprocess with its
own environment, so the tests exercise the same code path a deployment does: a client speaks
HTTP to the shim's port, the shim forwards, the stub records what actually arrived.

Run from the repository root:

    python -m unittest discover -s tests
"""
import gzip
import http.client
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stub_upstream  # noqa: E402

SHIM = Path(__file__).resolve().parent.parent / "shim.py"

# The shim requires a pattern, without a default. This one is an arbitrary fixture: the whole
# point of the variable is that the shape of the annotation is configuration.
PATTERN = r"\[note [0-9]+\]"
REFERENCE = {"type": "tool_reference", "tool_name": "Widget"}
# start_shim(target=UNSET) leaves SHIM_TARGET out of the environment entirely, so the shim
# falls back to its own default instead of the stub.
UNSET = object()

_stub_server = None
_stub_port = 0


def annotation(number: int) -> dict:
    """What a gateway may put beside a tool_reference, in the shape PATTERN describes."""
    return {"type": "text", "text": f"[note {number}]"}


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def setUpModule():
    global _stub_server, _stub_port
    _stub_port = _free_port()
    _stub_server = ThreadingHTTPServer(("127.0.0.1", _stub_port), stub_upstream.H)
    _stub_server.daemon_threads = True
    threading.Thread(target=_stub_server.serve_forever, daemon=True).start()


def tearDownModule():
    if _stub_server is not None:
        _stub_server.shutdown()
        _stub_server.server_close()


def post(port: int, path: str, body: bytes, headers=None):
    """POST raw bytes and return (status, headers dict, body bytes)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        request_headers = {"content-type": "application/json",
                           "content-length": str(len(body))}
        if headers:
            request_headers.update(headers)
        conn.request("POST", path, body=body, headers=request_headers)
        resp = conn.getresponse()
        data = resp.read()
        response_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, response_headers, data
    finally:
        conn.close()


def tool_result_request(tool_use_id: str, content) -> bytes:
    return json.dumps({
        "model": "m",
        "messages": [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": content},
            ]},
        ],
    }).encode()


def text_request() -> bytes:
    return json.dumps({"model": "m", "messages": [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]}]}).encode()


def sent_tool_result_content(raw: bytes):
    """The tool_result content as it arrived at the stub."""
    payload = json.loads(raw)
    return payload["messages"][0]["content"][0]["content"]


class ShimCase(unittest.TestCase):
    """Base case: owns a temp dir and one shim subprocess, started per test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.log = self.tmp / "shim-log.jsonl"
        self.proc = None
        self._stdout = None
        stub_upstream.reset()

    def tearDown(self):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        if self._stdout is not None:
            self._stdout.close()
        self._tmp.cleanup()

    def start_shim(self, mode="fix", capture_dir=None, target=None, bind=None):
        self.port = _free_port()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("SHIM_") and k != "CAPTURE_DIR"}
        env["SHIM_PORT"] = str(self.port)
        env["SHIM_STRIP_PATTERN"] = PATTERN
        if target is not UNSET:
            env["SHIM_TARGET"] = target or f"http://127.0.0.1:{_stub_port}/v1"
        env["SHIM_LOG"] = str(self.log)
        if mode is not None:
            env["SHIM_MODE"] = mode
        if bind is not None:
            env["SHIM_BIND"] = bind
        if capture_dir is not None:
            env["CAPTURE_DIR"] = str(capture_dir)
        self._stdout = open(self.tmp / "shim.out", "w")
        self.proc = subprocess.Popen([sys.executable, str(SHIM)], cwd=str(self.tmp),
                                     env=env, stdout=self._stdout,
                                     stderr=subprocess.STDOUT)
        self._wait_port()

    def _wait_port(self, timeout=20.0):
        deadline = time.time() + timeout
        while True:
            if self.proc.poll() is not None:
                raise AssertionError(
                    "shim exited early:\n" + (self.tmp / "shim.out").read_text())
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                return
            except OSError:
                if time.time() > deadline:
                    raise AssertionError(f"shim port {self.port} never opened")
                time.sleep(0.05)

    def log_records(self):
        if not self.log.exists():
            return []
        out = []
        for line in self.log.read_text().splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    def records(self, action):
        return [r for r in self.log_records() if r.get("action") == action]

    def wait_for_log(self, predicate, timeout=10.0):
        """Log writes for a response happen after the client already has the body; poll."""
        deadline = time.time() + timeout
        while True:
            matches = [r for r in self.log_records() if predicate(r)]
            if matches:
                return matches
            if time.time() > deadline:
                raise AssertionError(
                    "no matching log record; log was:\n"
                    + "\n".join(json.dumps(r) for r in self.log_records()))
            time.sleep(0.05)

    def wait_for(self, predicate, timeout=10.0):
        deadline = time.time() + timeout
        while not predicate():
            if time.time() > deadline:
                return False
            time.sleep(0.05)
        return True


class StripRule(ShimCase):
    # Request records (strip, unmatched) are written before the request is forwarded, so they
    # are complete by the time the client has its response.

    def test_annotation_beside_a_reference_is_removed(self):
        self.start_shim()
        body = tool_result_request("toolu_strip", [REFERENCE, annotation(7)])
        status, _, _ = post(self.port, "/v1/messages", body)
        self.assertEqual(200, status)
        self.assertEqual([REFERENCE], sent_tool_result_content(stub_upstream.LAST["body"]))
        record = self.records("strip")[0]
        self.assertEqual("toolu_strip", record["tool_use_id"])
        self.assertEqual([annotation(7)], record["removed"])
        self.assertEqual([REFERENCE], record["kept"])
        self.assertEqual([], self.records("unmatched"))

    def test_rewritten_body_does_not_depend_on_the_annotation(self):
        # A gateway may renumber its annotations from one turn to the next. What reaches the
        # API must not change with them, or every turn would miss the prompt cache.
        self.start_shim()
        post(self.port, "/v1/messages",
             tool_result_request("toolu_same", [REFERENCE, annotation(7)]))
        first = stub_upstream.LAST["body"]
        post(self.port, "/v1/messages",
             tool_result_request("toolu_same", [REFERENCE, annotation(12)]))
        self.assertEqual(first, stub_upstream.LAST["body"])

    def test_rewritten_body_carries_what_an_unannotated_one_does(self):
        # Once the gateway stops annotating, requests pass byte for byte. They must carry the
        # content the API has been seeing all along, so the cache survives that change too.
        self.start_shim()
        post(self.port, "/v1/messages",
             tool_result_request("toolu_same", [REFERENCE, annotation(7)]))
        rewritten = json.loads(stub_upstream.LAST["body"])
        clean = tool_result_request("toolu_same", [REFERENCE])
        post(self.port, "/v1/messages", clean)
        self.assertEqual(clean, stub_upstream.LAST["body"])
        self.assertEqual(json.loads(clean), rewritten)

    def test_unrecognised_block_is_kept_and_logged(self):
        self.start_shim()
        other = {"type": "text", "text": "something the pattern does not describe"}
        body = tool_result_request("toolu_mixed", [REFERENCE, annotation(7), other])
        status, _, _ = post(self.port, "/v1/messages", body)
        self.assertEqual(200, status)
        self.assertEqual([REFERENCE, other],
                         sent_tool_result_content(stub_upstream.LAST["body"]))
        self.assertEqual([annotation(7)], self.records("strip")[0]["removed"])
        record = self.records("unmatched")[0]
        self.assertEqual("toolu_mixed", record["tool_use_id"])
        self.assertEqual([other], record["blocks"])

    def test_pattern_must_match_the_whole_text(self):
        self.start_shim()
        partial = {"type": "text", "text": "[note 7] followed by real content"}
        body = tool_result_request("toolu_partial", [REFERENCE, partial])
        post(self.port, "/v1/messages", body)
        self.assertEqual(body, stub_upstream.LAST["body"])
        self.assertEqual([], self.records("strip"))
        self.assertEqual([partial], self.records("unmatched")[0]["blocks"])

    def test_block_with_any_other_key_is_kept(self):
        # A cache breakpoint on such a block must never be dropped silently.
        self.start_shim()
        marked = dict(annotation(7), cache_control={"type": "ephemeral"})
        body = tool_result_request("toolu_marked", [REFERENCE, marked])
        post(self.port, "/v1/messages", body)
        self.assertEqual(body, stub_upstream.LAST["body"])
        self.assertEqual([], self.records("strip"))
        self.assertEqual([marked], self.records("unmatched")[0]["blocks"])

    def test_tool_result_without_a_reference_passes_byte_for_byte(self):
        # Annotations elsewhere are the gateway's own business, even in the matching shape.
        self.start_shim()
        for tid, content in (("toolu_text", "plain text result"),
                             ("toolu_list", [annotation(7)])):
            body = tool_result_request(tid, content)
            self.assertEqual(200, post(self.port, "/v1/messages", body)[0])
            self.assertEqual(body, stub_upstream.LAST["body"])
        self.assertEqual([], self.records("strip") + self.records("unmatched"))


class RelayOutcomes(ShimCase):
    """Every relay leaves a record: how it ended and how long it took. The events a client
    reports as "retrying" (a dead upstream, a truncated stream) must be in the log, not only
    on stdout."""

    def test_relay_record_with_duration(self):
        self.start_shim()
        self.assertEqual(200, post(self.port, "/v1/messages", text_request())[0])
        record = self.wait_for_log(lambda r: r.get("action") == "relay")[0]
        self.assertEqual({"status": 200, "path": "/v1/messages",
                          "bytes": len(stub_upstream.SSE.encode())},
                         {k: record[k] for k in ("status", "path", "bytes")})
        self.assertIsInstance(record["duration_ms"], int)
        self.assertGreaterEqual(record["duration_ms"], 0)

    def test_unreachable_upstream_logs_a_transport_record(self):
        self.start_shim(target=f"http://127.0.0.1:{_free_port()}/v1")
        status, _, _ = post(self.port, "/v1/messages", json.dumps({"messages": []}).encode())
        self.assertEqual(502, status)
        record = self.wait_for_log(lambda r: r.get("action") == "transport")[0]
        self.assertEqual({"phase": "connect", "status": 502, "bytes": 0},
                         {k: record[k] for k in ("phase", "status", "bytes")})
        self.assertIn("ConnectionRefusedError", record["error"])

    def test_upstream_dying_mid_stream_logs_a_transport_record(self):
        self.start_shim()
        # The shim has already sent 200 when the upstream vanishes, so the client sees a
        # truncated chunked body: exactly what a CLI then reports as a retry. (http.client
        # raises before handing over the chunk it had buffered, hence 0 bytes relayed.)
        with self.assertRaises(http.client.IncompleteRead):
            post(self.port, "/v1/truncate", b"{}")
        record = self.wait_for_log(lambda r: r.get("action") == "transport")[0]
        self.assertEqual({"phase": "stream", "status": 200, "bytes": 0},
                         {k: record[k] for k in ("phase", "status", "bytes")})
        self.assertIn("IncompleteRead", record["error"])
        self.assertIsNone(self.proc.poll())  # still alive

    def test_client_leaving_mid_stream_logs_client_gone(self):
        self.start_shim()
        request = (b"POST /v1/slow HTTP/1.1\r\nhost: x\r\ncontent-length: 2\r\n"
                   b"content-type: application/json\r\n\r\n{}")
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        # linger 0: close() sends RST at once, so the shim's next write fails instead of
        # filling a kernel buffer nobody reads (a plain close is not prompt on every OS)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.sendall(request)
        sock.recv(4096)  # the status line and headers, maybe the first chunk
        sock.close()
        record = self.wait_for_log(lambda r: r.get("action") == "client-gone")[0]
        self.assertEqual({"status": 200, "path": "/v1/slow"},
                         {k: record[k] for k in ("status", "path")})
        self.assertIn("Error", record["error"])
        self.assertIsNone(self.proc.poll())


class Plumbing(ShimCase):

    def test_configured_bind_address_is_reported_and_serves_requests(self):
        self.start_shim(bind="0.0.0.0")
        self.assertIn(f"0.0.0.0:{self.port} ->", (self.tmp / "shim.out").read_text())
        status, _, data = post(self.port, "/v1/messages", text_request())
        self.assertEqual(200, status)
        self.assertEqual(stub_upstream.SSE.encode(), data)

    def test_target_defaults_to_the_anthropic_api(self):
        # Nothing leaves the machine here: the banner is printed before the listener starts,
        # and this test sends no request.
        self.start_shim(target=UNSET)
        banner = (self.tmp / "shim.out").read_text()
        self.assertIn(f"127.0.0.1:{self.port} -> https://api.anthropic.com/v1", banner)

    def test_request_without_changes_passes_byte_for_byte(self):
        self.start_shim()
        body = text_request()
        status, _, data = post(self.port, "/v1/messages", body)
        self.assertEqual(200, status)
        self.assertEqual(stub_upstream.SSE.encode(), data)
        self.assertEqual("/v1/messages", stub_upstream.LAST["path"])
        self.assertEqual(body, stub_upstream.LAST["body"])

    def test_target_prefix_is_never_doubled(self):
        # A gateway may drop the leading /v1 on one path and keep it on another.
        self.start_shim()
        for path in ("/messages", "/v1/messages/count_tokens"):
            post(self.port, path, text_request())
            self.assertEqual("/v1" + path.removeprefix("/v1"), stub_upstream.LAST["path"])

    def test_encoded_reply_is_relayed_as_is(self):
        self.start_shim()
        status, headers, data = post(self.port, "/v1/messages/count_tokens", text_request())
        self.assertEqual(200, status)
        self.assertEqual("gzip", headers.get("content-encoding"))
        self.assertEqual({"usage": {"input_tokens": 12345}}, json.loads(gzip.decompress(data)))

    def test_no_captures_when_capture_dir_is_unset(self):
        self.start_shim()
        self.assertEqual(200, post(self.port, "/v1/messages", text_request())[0])
        self.wait_for_log(lambda r: r.get("action") == "relay")
        self.assertEqual([], sorted(p.name for p in self.tmp.iterdir()
                                    if p.name.endswith("-req.json") or p.is_dir()))

    def test_captures_written_when_capture_dir_is_set(self):
        captures = self.tmp / "captures"
        self.start_shim(capture_dir=captures)
        body = text_request()
        self.assertEqual(200, post(self.port, "/v1/messages", body)[0])
        wanted = [captures / f"001-{name}"
                  for name in ("req.json", "req.body", "resp.json", "resp.body")]
        self.assertTrue(self.wait_for(lambda: all(p.exists() for p in wanted)),
                        f"missing: {[str(p) for p in wanted if not p.exists()]}")
        self.assertEqual(body, (captures / "001-req.body").read_bytes())

    def test_log_mode_records_but_does_not_rewrite(self):
        self.start_shim(mode="log")
        body = tool_result_request("toolu_logmode", [REFERENCE, annotation(7)])
        status, _, _ = post(self.port, "/v1/messages", body)
        self.assertEqual(200, status)
        self.assertEqual(body, stub_upstream.LAST["body"])
        record = self.records("strip")[0]
        self.assertEqual("log", record["mode"])
        self.assertEqual("toolu_logmode", record["tool_use_id"])


if __name__ == "__main__":
    unittest.main()
