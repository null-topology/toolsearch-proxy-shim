# toolsearch-proxy-shim

Keeps Claude Code's tool search — deferred tools and their `tool_reference` results — working
through an HTTP gateway that annotates requests on their way to the API.

## The problem

Claude Code echoes the result of a `ToolSearch` call back as a `tool_result` whose `content` is a
single block:

```json
{"type": "tool_reference", "tool_name": "SomeTool"}
```

The Messages API refuses any other block standing beside a `tool_reference` in the same content
array. Put a text block next to it and the request fails with 400:

```
Tool definitions/code execution functions cannot be mixed with other content
```

Gateways that sit between the harness and the API routinely annotate message content — adding a
marker of their own, so that a message can be referred to later, is a common design. An
annotation that lands inside a `tool_result.content` array looks like this:

```json
{"type": "text", "text": "[annotation added by the gateway]"}
```

and it turns every ToolSearch round trip into the 400 above. The session dies on its first use of
a deferred tool.

The failure is narrow. Tool definitions carrying `defer_loading` travel through a gateway fine;
only the `tool_reference` *result* is affected. The cause is an addition, not a loss — the same
body sent straight to the API returns 200.

Most setups never reach it. Claude Code turns tool search off by itself when
`ANTHROPIC_BASE_URL` points at a non-Anthropic host, unless
`_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL` is set, so a default install behind a gateway never
sends a `tool_reference` at all.

## What the shim does

One HTTP listener on `127.0.0.1` by default; set `SHIM_BIND` to change it. It sits behind the
gateway, on the way to the API:

```
Claude Code → gateway → shim → api.anthropic.com
```

In every `tool_result` whose `content` holds a `tool_reference` block, the shim removes the
gateway's annotations beside it. A block counts as an annotation only when all of this holds:

- it has exactly two keys, `type` and `text`;
- `type` is `text`;
- `text` matches `SHIM_STRIP_PATTERN` in full (Python `re.fullmatch`).

Any other block beside a reference stays where it is and is logged as `unmatched`. The API then
refuses the request with the 400 above, exactly as it would without the shim, and the log names
the block. A gateway that changes the shape of its annotation shows up at once, instead of the
shim silently cutting content it does not recognise.

Everything else — other tool results, other requests, headers, streaming response bodies — passes
through untouched, and a request with nothing to remove is forwarded byte for byte.

### What the shim does not touch

Only a `tool_result` that holds a `tool_reference` is inspected, and such a block exists only in
the echo of a ToolSearch call. An ordinary tool result — a Bash run, a file read, an MCP tool
returning a large payload — carries text or images, never a `tool_reference`, so the shim skips
it entirely. It reaches the API exactly as the gateway assembled it, annotations included, and
whatever the gateway builds on those annotations keeps working.

The loss is confined to the ToolSearch echoes: an annotation the gateway placed there is dropped,
so anything keyed on it no longer applies to those particular messages. The rewrite happens
*after* the gateway, on the way to the API, so the gateway's own copy of the conversation is
unaffected — it never sees the edit.

### Prompt caching

The rewrite is built not to disturb the prompt cache:

- It is deterministic: the same request always yields the same body. The removed annotation never
  reaches the API, so a gateway renumbering its annotations from one turn to the next changes
  nothing the API sees.
- It applies to every request, including the ones a gateway issues on its own behalf, so those
  share the cached prefix of the conversation they belong to.
- A block with any key besides `type` and `text` — a `cache_control` breakpoint above all — is
  never removed.
- A rewritten body is re-serialized, which changes JSON formatting only; the prompt cache keys on
  content. Once the gateway stops annotating, requests pass byte for byte and carry the same
  content as before, so the cache survives that change as well.

## Install / run

No dependencies. One file, standard library only, Python 3.10+.

Run a tagged release straight from GitHub with [uv](https://docs.astral.sh/uv/) (nothing to
clone, nothing to publish; the configuration is all environment variables, see below):

```sh
uvx --from git+https://github.com/null-topology/toolsearch-proxy-shim@v0.3.0 toolsearch-proxy-shim
```

or install it once as a tool (`uv tool install` the same `--from` spec) so that
`toolsearch-proxy-shim` is on `PATH`. Or clone and run the file directly:

```sh
git clone https://github.com/null-topology/toolsearch-proxy-shim.git
cd toolsearch-proxy-shim
python shim.py
```

### Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `SHIM_MODE` | `fix` | `fix` removes the annotations; `log` writes the same records and rewrites nothing (the diagnostic mode). |
| `SHIM_BIND` | `127.0.0.1` | Address the listener binds. Use `0.0.0.0` when the shim runs in a container and its port is published. |
| `SHIM_PORT` | required | Port of the listener, the one the gateway forwards to. |
| `SHIM_TARGET` | `https://api.anthropic.com/v1` | Where the shim forwards. Override it to put another compatible upstream behind the shim. |
| `SHIM_STRIP_PATTERN` | required, no default | Python regular expression matched against the whole text of a block beside a `tool_reference`. Describe the gateway's annotation as tightly as you can. |
| `SHIM_LOG` | `shim-log.jsonl` | The jsonl record file. |
| `CAPTURE_DIR` | unset | When set, every request (as forwarded, after the rewrite) and response is also written under it as `NNN-req.json`, `NNN-req.body`, `NNN-resp.json`, `NNN-resp.body`. Unset or empty: nothing is written to disk — these files hold whole conversations. |

Secrets are never written: `authorization`, `x-api-key`, `cookie` and `proxy-authorization` are
stored as `<redacted>`, and token-shaped strings inside logged blocks are replaced too.

### Pointing the gateway at the shim

The wiring needs a gateway that can be told which upstream to forward to. Point it at the shim's
port, and point Claude Code at the gateway route that does so. How the upstream is selected — a
path prefix, a header, a config file — is your gateway's business; consult its documentation.

One quirk is worth knowing in advance. A gateway may normalise the harness's leading `/v1` on
some paths and leave it on others (`/messages` against `/v1/messages/count_tokens`).
`SHIM_TARGET` therefore carries the `/v1` itself, and the shim never prepends a prefix the path
already carries. A `count_tokens` call answering 404 is this mismatch.

```sh
SHIM_MODE=fix \
SHIM_PORT=9002 \
SHIM_TARGET=https://api.anthropic.com/v1 \
SHIM_STRIP_PATTERN='<regular expression for the gateway annotation>' \
SHIM_LOG=shim-log.jsonl \
python3 shim.py
```

For a gateway that appends blocks such as `[note 17]`, the pattern would be
`SHIM_STRIP_PATTERN='\[note [0-9]+\]'`. On start the shim prints one banner line naming the
listener, the target, the pattern and the log path.

### Pointing Claude Code at the gateway

```sh
ANTHROPIC_BASE_URL='http://127.0.0.1:9000/<gateway route naming http://127.0.0.1:9002 as upstream>' claude
```

(`9000` stands for the gateway's own port; both ports are arbitrary, substitute yours.) Tool
search only turns on when Claude Code treats that base URL as first party, so the session also
needs `_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL` set — without it there is no `tool_reference`
traffic and nothing for the shim to repair. That variable is internal to the CLI and may change
or disappear without notice.

## Reading the output

Each relayed request prints one line on stdout:

```
[003] POST /messages -> 200 8412ms
```

`[NNN]` is the request counter, then the status and the time from the first byte of the request
to the last byte of the response.

The jsonl file holds one record per line, each with `ts`, `mode`, `request` and `action`:

| `action` | Written when |
| --- | --- |
| `strip` | Annotations were found beside a reference; carries `tool_use_id`, `removed` and `kept`. In `log` mode nothing was removed. |
| `unmatched` | Blocks beside a reference that are not annotations were left in place; carries `tool_use_id` and `blocks`. The API is about to answer 400. |
| `relay` | One response delivered in full; carries `status`, `path`, `bytes` and `duration_ms`. |
| `transform-error` | Inspecting or repairing a request body raised; the body was forwarded unchanged. |
| `transport` | The connection to the upstream failed: `phase: "connect"` before any response (the client got a 502), or `phase: "stream"` mid-body (the client got a truncated stream). Carries `error`, `bytes` already relayed and `duration_ms`. |
| `client-gone` | The client closed its connection while the response was still streaming; carries `status`, `bytes`, `error`, `duration_ms`. |

A client that reports "retrying" or "waiting for the API" has usually hit one of the last two.
When neither appears for that moment, the break was before the shim — in the gateway or between
it and the client.

What the pattern did not recognise:

```sh
jq -c 'select(.action=="unmatched")' shim-log.jsonl
```

## Upgrading from 0.2

- The IN listener is gone, and with it the `restore` rule, `SHIM_IN_PORT`, `IN_TARGET` and
  `SHIM_AUTH_TOKEN`. Point Claude Code at the gateway directly.
- `SHIM_OUT_PORT` is now `SHIM_PORT`, and `OUT_TARGET` is now `SHIM_TARGET`.
- `SHIM_STRIP_PATTERN` is required: 0.2 removed everything beside a reference, 0.3 removes only
  what the pattern describes.
- Token accounting is gone: `REPLAY_PROMPT`, `REPLAY_MARKER` and the `usage`, `usage-error`,
  `remember`, `restore`, `observe` and `reject` records.
- Captures are written straight under `CAPTURE_DIR`, no longer under `in/` and `out/`.

## Limitations

- The strip rule is only as good as the pattern. A gateway that changes the shape of its
  annotation makes requests fail with the 400 again; the `unmatched` record names the new block,
  and the pattern has to follow it.
- The listener has no authentication. Keep it on loopback or a private network: whoever reaches
  it can send requests to the upstream behind it.
- The shim does not look into responses; it relays their bytes.
- Not affiliated with, endorsed by or supported by any gateway vendor or by Anthropic.

## Tests

```sh
python -m unittest discover -s tests
```

The suite starts a stub upstream in-process and the shim as a real subprocess, then drives its
listener over HTTP. No network access and no API key required.

## License

MIT. See [LICENSE](LICENSE).
