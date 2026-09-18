# toolsearch-proxy-shim

Keeps Claude Code's tool search — deferred tools and their `tool_reference` results — working
through an HTTP gateway that rewrites requests on their way to the API, and accounts for the
tokens such a gateway spends on requests of its own.

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

One process, two HTTP listeners on `127.0.0.1`. The gateway is wrapped on both sides:

```
Claude Code → IN (shim) → gateway → OUT (shim) → api.anthropic.com
```

**IN** sees the request as Claude Code wrote it. It records, per `tool_use_id`, the
`tool_reference` blocks found inside each `tool_result.content` array, and forwards the request
byte for byte. It changes nothing.

**OUT** sees the same request after the gateway has been through it, and applies two rules to
every `tool_result`:

1. **strip** — the content array still contains `tool_reference` blocks: keep only those, drop
   everything the gateway put beside them.
2. **restore** — the content array has no `tool_reference` but the `tool_use_id` is one IN
   remembered: put the remembered blocks back as the whole content.

Everything else — other requests, headers, streaming response bodies — passes through untouched.
Rule 1 covers the common case, a gateway that appends; rule 2 exists for one that replaces the
reference outright.

### What the shim does not touch

Both rules key on one thing: a `tool_reference` block inside `tool_result.content`. Such a block
exists only in the echo of a ToolSearch call. An ordinary tool result — a Bash run, a file read,
an MCP tool returning a large payload — carries text or images, never a `tool_reference`, so the
rules skip it entirely. It reaches the API exactly as the gateway assembled it, annotations
included, and whatever the gateway builds on those annotations keeps working.

The loss is confined to the ToolSearch echoes: an annotation the gateway placed there is dropped,
so anything keyed on it no longer applies to those particular messages. What stands behind them
is a single `tool_reference` block of a few dozen tokens. The rewrite happens *after* the
gateway, on the way to the API, so the gateway's own copy of the conversation and its accounting
are unaffected — it never sees the edit.

### Token accounting

On top of the repair, OUT keeps account of tokens. Every OUT response is parsed for the API's
`usage` (the SSE `message_start` / `message_delta` events, or the JSON body of a non-streamed
reply) and logged with an `origin`:

- `harness` — the request came from Claude Code through IN;
- `gateway-replay` — a hidden request the gateway issued on its own behalf, one the harness never
  sees, recognised by the `REPLAY_PROMPT` / `REPLAY_MARKER` you configure;
- `count_tokens` — a `/v1/messages/count_tokens` call.

Running totals per origin are logged with each record. A gateway's own requests enter the shim
only on the OUT side, so an OUT request count larger than the IN count is exactly those.

## Install / run

No dependencies. One file, standard library only, Python 3.10+.

```
git clone https://github.com/null-topology/toolsearch-proxy-shim.git
cd toolsearch-proxy-shim
```

### Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `SHIM_MODE` | `fix` | `fix` applies the two repair rules; `log` only records what the gateway sent, rewriting nothing (the diagnostic mode). |
| `SHIM_IN_PORT` | required | Port of the IN listener, the one Claude Code talks to. |
| `SHIM_OUT_PORT` | required | Port of the OUT listener, the one the gateway forwards to. |
| `IN_TARGET` | required | Where IN forwards: the gateway URL that names OUT as the upstream. |
| `OUT_TARGET` | `https://api.anthropic.com/v1` | Where OUT forwards. Override it to put another compatible upstream behind the shim. |
| `SHIM_LOG` | `shim-log.jsonl` | The jsonl record file. |
| `REPLAY_PROMPT` | required, no default | Opening words of the gateway's own hidden request, matched against the last user message. An empty value switches this signal off. |
| `REPLAY_MARKER` | required, no default | A marker the gateway places inside that request. An empty value switches this signal off. |
| `CAPTURE_DIR` | unset | When set, every request and response is also written under `<dir>/in` and `<dir>/out`. Unset or empty: nothing is written to disk — these files hold whole conversations. Usage accounting works either way. |

Secrets are never written: `authorization`, `x-api-key`, `cookie` and `proxy-authorization` are
stored as `<redacted>`, and token-shaped strings inside logged blocks are replaced too.

### Pointing the gateway at the OUT port

The wiring needs a gateway that can be told which upstream to forward to. Point it at the shim's
OUT port, and point `IN_TARGET` at the gateway route that does so. How the upstream is selected —
a path prefix, a header, a config file — is your gateway's business; consult its documentation.

One quirk is worth knowing in advance. A gateway may normalise the harness's leading `/v1` on
some paths and leave it on others (`/messages` against `/v1/messages/count_tokens`). `OUT_TARGET`
therefore carries the `/v1` itself, and the shim never prepends a prefix the path already carries.
A `count_tokens` call answering 404 is this mismatch.

```sh
SHIM_MODE=fix \
SHIM_IN_PORT=9001 \
SHIM_OUT_PORT=9002 \
IN_TARGET='http://127.0.0.1:9000/<gateway route naming http://127.0.0.1:9002 upstream>' \
OUT_TARGET=https://api.anthropic.com/v1 \
SHIM_LOG=shim-log.jsonl \
REPLAY_PROMPT='<opening words of the gateway hidden request>' \
REPLAY_MARKER='<marker the gateway puts in that request>' \
python3 shim.py
```

(`9000` stands for the gateway's own port; all three are arbitrary, substitute yours.) On start
the shim prints one banner line naming both listeners, both targets and the log path.

### Pointing Claude Code at the IN port

```sh
ANTHROPIC_BASE_URL=http://127.0.0.1:9001 claude
```

Tool search only turns on when Claude Code treats that base URL as first party, so the session
also needs `_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL` set — without it there is no
`tool_reference` traffic and nothing for the shim to repair. That variable is internal to the
CLI and may change or disappear without notice.

## Reading the output

Each relayed request prints one line on stdout:

```
[out 003] POST /v1/messages -> 200 origin=harness in=491 cache_w=624 cache_r=389040 out=712 | total[harness] n=3 in=1473 cache_w=1872 cache_r=1167120 out=2136
```

`[in NNN]` / `[out NNN]` is the side and its request counter; `in / cache_w / cache_r / out` are
`input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens` for
that response, then the running total for that origin. IN lines carry no usage — only OUT sees
responses from the API.

The jsonl file holds one record per line, each with `ts`, `mode`, `side` and `request`. The
record kinds:

| `action` | Written when |
| --- | --- |
| `remember` | IN saw a `tool_reference` for a `tool_use_id` for the first time. |
| `strip` | OUT found extra blocks beside the reference; carries `removed` and `kept`. |
| `restore` | OUT found the reference gone for a remembered id; carries `erased_content` and `restored`. |
| `observe` | `log` mode only: a remembered `tool_result` exactly as the gateway sent it. |
| `usage` | One OUT response accounted for; carries `origin`, `status`, `usage` and `totals`. `usage` is `null` when the reply carried none (errors, unknown shapes). |
| `usage-error` | The response body could not be parsed for usage. |
| `transform-error` | Inspecting or repairing a request body raised; the body was forwarded unchanged. |

The running totals, per origin:

```sh
jq -c 'select(.action=="usage") | .totals' shim-log.jsonl | tail -1
```

## Limitations

- The **restore** rule has never been exercised in the wild — every gateway behaviour behind this
  shim appended to the reference rather than erasing it. The rule is covered by tests, not by
  production traffic.
- Replay detection is only as good as the two strings you configure: it matches `REPLAY_PROMPT`
  against the opening of the last user message and looks for `REPLAY_MARKER` anywhere in it.
  Either signal is enough; a request with neither is accounted as `harness`. A gateway that
  rewords its hidden request silently stops being recognised.
- A gateway that changes the shape of its annotation can likewise break the strip rule's premise.
- The shim does not touch streaming responses; it only reads them for usage and relays the bytes.
- Remembered references are held in memory for the lifetime of the process, keyed by
  `tool_use_id`; nothing evicts them.
- Not affiliated with, endorsed by or supported by any gateway vendor or by Anthropic.

## Tests

```sh
python -m unittest discover -s tests
```

The suite starts a stub upstream in-process and the shim as a real subprocess, then drives both
listeners over HTTP. No network access and no API key required.

## License

MIT. See [LICENSE](LICENSE).
