"""End-to-end tests for shim.py.

A stub upstream runs in a thread in this process; the shim runs as a real subprocess with its
own environment, so the tests exercise the same code path a deployment does: a client speaks
HTTP to the IN or OUT port, the shim forwards, the stub records what actually arrived.

Run from the repository root:

    python -m unittest discover -s tests
"""
import http.client
import json
import os
import socket
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

# The shim requires both, without a default. These are arbitrary fixtures: the whole point of the
# variables is that the wording is configuration, so any string does.
REPLAY_PROMPT = "TEST-REPLAY-OPENING"
REPLAY_MARKER = "TEST-REPLAY-MARKER"
# Whatever a gateway may add beside a tool_reference. The shim keys on the reference, never on
# the shape of what stands next to it.
ANNOTATION = "[annotation added by the gateway]"
# start_shim(out_target=UNSET) leaves OUT_TARGET out of the environment entirely, so the shim
# falls back to its own default instead of the stub.
UNSET = object()

_stub_server = None
_stub_port = 0


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


def post(port: int, path: str, body: bytes):
    """POST raw bytes and return (status, headers dict, body bytes)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        conn.request("POST", path, body=body,
                     headers={"content-type": "application/json",
                              "content-length": str(len(body))})
        resp = conn.getresponse()
        data = resp.read()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, headers, data
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

    def start_shim(self, mode="fix", capture_dir=None, log_path=None,
                   replay_prompt=None, replay_marker=None, out_target=None, bind=None):
        self.in_port = _free_port()
        self.out_port = _free_port()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("SHIM_") and k not in ("CAPTURE_DIR", "IN_TARGET",
                                                          "OUT_TARGET", "REPLAY_PROMPT",
                                                          "REPLAY_MARKER")}
        env["SHIM_IN_PORT"] = str(self.in_port)
        env["SHIM_OUT_PORT"] = str(self.out_port)
        env["REPLAY_PROMPT"] = REPLAY_PROMPT if replay_prompt is None else replay_prompt
        env["REPLAY_MARKER"] = REPLAY_MARKER if replay_marker is None else replay_marker
        # No gateway in the tests: IN forwards straight to OUT, which forwards to the stub.
        env["IN_TARGET"] = f"http://127.0.0.1:{self.out_port}/v1"
        if out_target is not UNSET:
            env["OUT_TARGET"] = out_target or f"http://127.0.0.1:{_stub_port}/v1"
        if log_path is None:
            log_path = self.log
        env["SHIM_LOG"] = str(log_path)
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
        self._wait_ports()

    def _wait_ports(self, timeout=20.0):
        deadline = time.time() + timeout
        for port in (self.in_port, self.out_port):
            while True:
                if self.proc.poll() is not None:
                    raise AssertionError(
                        "shim exited early:\n" + (self.tmp / "shim.out").read_text())
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=1).close()
                    break
                except OSError:
                    if time.time() > deadline:
                        raise AssertionError(f"shim port {port} never opened")
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


class RepairRules(ShimCase):

    def test_strip_keeps_only_the_reference_block(self):
        self.start_shim()
        body = tool_result_request("toolu_strip", [
            {"type": "tool_reference", "tool_name": "Widget"},
            {"type": "text", "text": ANNOTATION},
        ])
        status, _, _ = post(self.out_port, "/v1/messages", body)
        self.assertEqual(200, status)
        self.assertEqual(
            [{"type": "tool_reference", "tool_name": "Widget"}],
            sent_tool_result_content(stub_upstream.LAST["body"]))
        record = self.wait_for_log(lambda r: r.get("action") == "strip")[0]
        self.assertEqual("toolu_strip", record["tool_use_id"])
        self.assertEqual([{"type": "text", "text": ANNOTATION}], record["removed"])

    def test_restore_puts_the_remembered_reference_back(self):
        self.start_shim()
        reference = {"type": "tool_reference", "tool_name": "Widget"}
        # IN sees the harness's own request and remembers the reference for this id.
        status, _, _ = post(self.in_port, "/v1/messages",
                            tool_result_request("toolu_restore", [reference]))
        self.assertEqual(200, status)
        self.assertEqual([reference], sent_tool_result_content(stub_upstream.LAST["body"]))
        self.wait_for_log(lambda r: r.get("action") == "remember")

        # Now the same id comes back on the OUT side with the reference erased.
        stub_upstream.reset()
        status, _, _ = post(self.out_port, "/v1/messages", tool_result_request(
            "toolu_restore", [{"type": "text", "text": ANNOTATION}]))
        self.assertEqual(200, status)
        self.assertEqual([reference], sent_tool_result_content(stub_upstream.LAST["body"]))
        record = self.wait_for_log(lambda r: r.get("action") == "restore")[0]
        self.assertEqual("toolu_restore", record["tool_use_id"])
        self.assertEqual([reference], record["restored"])

    def test_unknown_tool_result_passes_byte_for_byte(self):
        self.start_shim()
        body = tool_result_request("toolu_unknown", "plain text result")
        status, _, _ = post(self.out_port, "/v1/messages", body)
        self.assertEqual(200, status)
        self.assertEqual(body, stub_upstream.LAST["body"])
        self.assertEqual([], [r for r in self.log_records()
                              if r.get("action") in ("strip", "restore")])


class UsageAccounting(ShimCase):

    def test_usage_from_an_sse_reply(self):
        self.start_shim()
        body = json.dumps({"model": "m", "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]}]}).encode()
        status, _, _ = post(self.out_port, "/v1/messages", body)
        self.assertEqual(200, status)
        record = self.wait_for_log(lambda r: r.get("action") == "usage")[0]
        self.assertEqual("harness", record["origin"])
        self.assertEqual({"input_tokens": 491,
                          "cache_creation_input_tokens": 624,
                          "cache_read_input_tokens": 389040,
                          "output_tokens": 712}, record["usage"])
        self.assertEqual(1, record["totals"]["harness"]["requests"])

    def test_origin_gateway_replay_by_prompt(self):
        self.start_shim()
        body = json.dumps({"model": "m", "messages": [
            {"role": "user", "content": [{"type": "text", "text": "x"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "y"}]},
            {"role": "user", "content": [{"type": "text", "text":
                f"{REPLAY_PROMPT} followed by whatever else the gateway asks for"}]},
        ]}).encode()
        status, _, _ = post(self.out_port, "/v1/messages", body)
        self.assertEqual(200, status)
        record = self.wait_for_log(lambda r: r.get("action") == "usage")[0]
        self.assertEqual("gateway-replay", record["origin"])

    def test_origin_gateway_replay_by_marker_alone(self):
        # A gateway may word the request in a way REPLAY_PROMPT does not cover; the marker on
        # its own is enough to catch it. Either signal suffices.
        self.start_shim()
        body = json.dumps({"model": "m", "messages": [
            {"role": "user", "content": [{"type": "text", "text": "x"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "y"}]},
            {"role": "user", "content": [{"type": "text", "text":
                "An opening this shim was never told about.\n\n"
                f"{REPLAY_MARKER} further down the same message"}]},
        ]}).encode()
        status, _, _ = post(self.out_port, "/v1/messages", body)
        self.assertEqual(200, status)
        record = self.wait_for_log(lambda r: r.get("action") == "usage")[0]
        self.assertEqual("gateway-replay", record["origin"])

    def test_empty_replay_variables_switch_the_detection_off(self):
        # Without the emptiness guard, "".startswith and "" in text match every request and
        # everything would be misfiled as a replay.
        self.start_shim(replay_prompt="", replay_marker="")
        body = json.dumps({"model": "m", "messages": [
            {"role": "user", "content": [{"type": "text", "text":
                f"{REPLAY_PROMPT} anything at all {REPLAY_MARKER}"}]},
        ]}).encode()
        status, _, _ = post(self.out_port, "/v1/messages", body)
        self.assertEqual(200, status)
        record = self.wait_for_log(lambda r: r.get("action") == "usage")[0]
        self.assertEqual("harness", record["origin"])

    def test_out_target_defaults_to_the_anthropic_api(self):
        # Nothing leaves the machine here: the banner is printed before either listener starts,
        # and this test sends no request.
        self.start_shim(out_target=UNSET)
        banner = (self.tmp / "shim.out").read_text()
        self.assertIn(f"OUT 127.0.0.1:{self.out_port} -> https://api.anthropic.com/v1", banner)

    def test_origin_count_tokens(self):
        self.start_shim()
        body = json.dumps({"model": "m", "messages": [
            {"role": "user", "content": "hello"}]}).encode()
        status, headers, _ = post(self.out_port, "/v1/messages/count_tokens", body)
        self.assertEqual(200, status)
        self.assertEqual("gzip", headers.get("content-encoding"))
        record = self.wait_for_log(lambda r: r.get("action") == "usage")[0]
        self.assertEqual("count_tokens", record["origin"])
        self.assertEqual(12345, record["usage"]["input_tokens"])

    def test_reply_without_usage_logs_null(self):
        self.start_shim()
        status, _, _ = post(self.out_port, "/v1/nothing", json.dumps({"messages": []}).encode())
        self.assertEqual(404, status)
        record = self.wait_for_log(lambda r: r.get("action") == "usage")[0]
        self.assertIsNone(record["usage"])
        self.assertEqual(404, record["status"])
        self.assertIsNone(self.proc.poll())  # still alive


class Plumbing(ShimCase):

    def test_configured_bind_address_is_reported_and_serves_requests(self):
        self.start_shim(bind="0.0.0.0")
        banner = (self.tmp / "shim.out").read_text()
        self.assertIn(f"IN 0.0.0.0:{self.in_port}", banner)
        self.assertIn(f"OUT 0.0.0.0:{self.out_port}", banner)

        body = json.dumps({"model": "m", "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]}]}).encode()
        status, _, data = post(self.in_port, "/v1/messages", body)
        self.assertEqual(200, status)
        self.assertEqual(stub_upstream.SSE.encode(), data)

    def test_in_to_out_chain_returns_the_body_unchanged(self):
        self.start_shim()
        body = json.dumps({"model": "m", "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]}]}).encode()
        status, _, data = post(self.in_port, "/v1/messages", body)
        self.assertEqual(200, status)
        self.assertEqual(stub_upstream.SSE.encode(), data)
        self.assertEqual("/v1/messages", stub_upstream.LAST["path"])
        self.assertEqual(body, stub_upstream.LAST["body"])

    def test_no_captures_when_capture_dir_is_unset(self):
        self.start_shim()
        body = json.dumps({"model": "m", "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]}]}).encode()
        self.assertEqual(200, post(self.in_port, "/v1/messages", body)[0])
        self.wait_for_log(lambda r: r.get("action") == "usage")
        self.assertFalse((self.tmp / "captures").exists())
        self.assertEqual([], sorted(p.name for p in self.tmp.iterdir()
                                    if p.name.endswith("-req.json") or p.is_dir()))

    def test_captures_written_when_capture_dir_is_set(self):
        captures = self.tmp / "captures"
        self.start_shim(capture_dir=captures)
        body = json.dumps({"model": "m", "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]}]}).encode()
        self.assertEqual(200, post(self.in_port, "/v1/messages", body)[0])
        wanted = [captures / "in" / "001-req.json", captures / "in" / "001-req.body",
                  captures / "out" / "001-req.json", captures / "out" / "001-resp.body"]
        self.assertTrue(self.wait_for(lambda: all(p.exists() for p in wanted)),
                        f"missing: {[str(p) for p in wanted if not p.exists()]}")
        self.assertEqual(body, (captures / "out" / "001-req.body").read_bytes())

    def test_log_mode_records_but_does_not_rewrite(self):
        self.start_shim(mode="log")
        content = [
            {"type": "tool_reference", "tool_name": "Widget"},
            {"type": "text", "text": ANNOTATION},
        ]
        body = tool_result_request("toolu_logmode", content)
        status, _, _ = post(self.out_port, "/v1/messages", body)
        self.assertEqual(200, status)
        self.assertEqual(body, stub_upstream.LAST["body"])
        self.assertEqual(content, sent_tool_result_content(stub_upstream.LAST["body"]))
        record = self.wait_for_log(lambda r: r.get("action") == "strip")[0]
        self.assertEqual("log", record["mode"])
        self.assertEqual("toolu_logmode", record["tool_use_id"])


if __name__ == "__main__":
    unittest.main()
