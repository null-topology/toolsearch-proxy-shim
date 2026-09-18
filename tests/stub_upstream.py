"""Stub API upstream for testing the shim.

POST /v1/messages              -> SSE stream with message_start usage and message_delta output
POST /v1/messages/count_tokens -> gzip-encoded JSON {"usage": {"input_tokens": N}}
Any other path                 -> 404 JSON without usage.

Every request is recorded in LAST so a test can assert on what actually arrived here.
"""
import gzip
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

SSE = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"id":"msg_x","usage":{"input_tokens":491,'
    '"cache_creation_input_tokens":624,"cache_read_input_tokens":389040,"output_tokens":1}}}\n\n'
    'event: content_block_start\n'
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
    'event: message_delta\n'
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":712}}\n\n'
    'event: message_stop\n'
    'data: {"type":"message_stop"}\n\n'
)

# The last request this stub received: {"path": str, "body": bytes, "headers": dict}.
LAST: dict = {"path": None, "body": b"", "headers": {}}


def reset() -> None:
    LAST.update({"path": None, "body": b"", "headers": {}})


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n)
        LAST.update({"path": self.path, "body": raw,
                     "headers": {k.lower(): v for k, v in self.headers.items()}})
        if self.path.endswith("/count_tokens"):
            body = gzip.compress(json.dumps({"usage": {"input_tokens": 12345}}).encode())
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-encoding", "gzip")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.endswith("/messages"):
            body = SSE.encode()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps({"type": "error", "error": {"message": "nope"}}).encode()
        self.send_response(404)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
