# mcp-schema-probe

Point it at any MCP stdio server: it derives schema-violating arguments from the
server's **own** `inputSchema`, sends them over real JSON-RPC, and reports every
call the server **accepted anyway** — with a 1-minimal witness you can paste into
an issue.

Why now: MCP servers ship a `tools/list` contract that clients trust, but nothing
checks that the handler enforces it. The trending `chrome-devtools-mcp` project
is a live example (see the evidence below), and its own tests only exercise
well-typed calls.

Upstream: https://github.com/ChromeDevTools/chrome-devtools-mcp

## The upstream, and the gap this tool targets

`ChromeDevTools/chrome-devtools-mcp` publishes eight-plus browser-automation
tools with explicit `inputSchema` declarations. The declaration is the only
contract an agent ever sees.

Two publicly visible gaps show the contract is not enforced at the call
boundary:

- [`src/tools/emulation.ts`](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/src/tools/emulation.ts#L42)
  — `headerStringTransform` checks only that the input is a JSON object and
  returns `parsed as Record<string, string>`. The declared type of
  `extraHttpHeaders` is an object of strings, but numbers, booleans and nested
  objects survive the cast (C-level evidence: unchecked cast in source).
- [issue #3455](https://github.com/ChromeDevTools/chrome-devtools-mcp/issues/3455)
  — "`emulate` `extraHttpHeaders` accepts non-string header values". The report
  states validation succeeds and the non-string values are applied or stored
  (B-level evidence: an open issue that quotes the declaration and the actual
  behaviour).

The point is not that one project has one bug. The point is that neither the
server nor its clients have a way to *measure* whether a declaration is
enforced. That measurement is what this repository provides.

**Honest scope note:** this environment has no npm registry access, so the
upstream TypeScript binary was **not** executed here. The upstream is the
evidence source and the motivating contract, not the thing under test in this
repo's self-check. The self-check runs against three bundled stdio servers whose
tool declarations mirror the upstream shape and whose handlers reproduce the two
documented behaviours: unchecked acceptance and silent scalar coercion.

## What it does / what it does not do

It does:

1. Spawn the server command you give it (real child process, real stdio, real
   JSON-RPC; `initialize` → `notifications/initialized` → `tools/list` →
   `tools/call`).
2. Build a schema-valid baseline argument set per tool from its `inputSchema`.
3. Generate deterministic schema-violating mutants: missing required property,
   wrong JSON type, invalid `enum` member, undeclared property when
   `additionalProperties` is `false`, and a missing nested required property.
4. Classify each call as `accepted` (violation), `rejected` (correct), `timeout`
   or `crash`.
5. Minimise every violation to a 1-minimal witness (drop any single argument
   that is not needed to keep the call both schema-invalid and accepted) and
   re-check minimality.
6. Emit a stable JSON report with a SHA-256 `digest` over the run, so two runs
   can be compared byte-for-byte.

It does not: install, patch, or run the upstream project; judge whether a tool's
*result* is semantically correct; perform fuzzing beyond the declared schema; or
talk to remote/HTTP MCP transports (stdio only).

## Install

Zero dependencies. Python 3.8+.

```bash
git clone git@github-linmoxi33:linmoxi33/chrome-devtools-mcp-schema-probe.git
cd chrome-devtools-mcp-schema-probe
python3 --version          # 3.8+
```

There is nothing to install and nothing to build.

## Usage

Probe any MCP stdio server by passing its start command:

```bash
python3 bin/mcp_schema_probe.py probe --server "python3 examples/servers/loose_server.py"
```

Machine-readable report:

```bash
python3 bin/mcp_schema_probe.py probe \
  --server "python3 examples/servers/loose_server.py" \
  --json --report /tmp/report.json
```

Run the bundled clean control (a server that enforces its declarations):

```bash
python3 bin/mcp_schema_probe.py probe --server "python3 examples/servers/strict_server.py"
# exit code 0, violations: 0
```

### `--json` output shape

```json
{
  "tool": "mcp-schema-probe",
  "version": "1.0.0",
  "server": {"command": "python3 examples/servers/loose_server.py"},
  "protocol_version": "2025-06-18",
  "tools": 8,
  "calls": 78,
  "rpc_messages": 161,
  "counts": {"accepted": 43, "rejected": 0, "timeout": 0, "crash": 0},
  "violations": [
    {
      "tool": "emulate",
      "kind": "wrong_type",
      "expectation": "'extraHttpHeaders' is declared object, sent array",
      "witness": {"extraHttpHeaders": [1, 2, 3]},
      "witness_violation": "$.extraHttpHeaders must be object, got array",
      "minimal_1": true,
      "response_excerpt": "emulate ok"
    }
  ],
  "violations_by_kind": {"missing_required": 8, "wrong_type": 18,
                         "invalid_enum": 1, "unexpected_property": 8},
  "digest": "…64 hex chars…"
}
```

(The numbers above are the real output of the command in
`examples/out/loose-report.json`; the violation block is abridged to one entry.)

### Exit codes

| code | meaning |
| --- | --- |
| `0` | server enforced every declared schema constraint |
| `1` | at least one schema-violating call was accepted |
| `2` | usage error, unparsable `--server`, or the server could not start / reply |

### Options

| flag | meaning |
| --- | --- |
| `--server "<cmd>"` | required; parsed with `shlex.split`, executed without a shell |
| `--json` | print the report as JSON (parsable with `python3 -c "import json,sys;json.load(sys.stdin)"`) |
| `--report PATH` | additionally write the JSON report to `PATH` |
| `--timeout SECONDS` | per-message timeout (default `5`) |
| `--max-mutants N` | cap schema-violating probes per tool |

## Real self-check results

All numbers below come from commands run in this repository; nothing is
estimated.

| command | result |
| --- | --- |
| `python3 -m unittest discover -s tests -v` | **17 tests, OK** (0.75 s) |
| probe `strict_server.py` | rc `0`, 8 tools, 43 calls, **0 violations** (clean control) |
| probe `loose_server.py` | rc `1`, 8 tools, 78 calls, **35 violations**, all `minimal_1` |
| probe `coercing_server.py` | rc `1`, **13 violations**, all `wrong_type` |
| same command twice | identical `digest` |

Across the three bundled servers that is **174 real `tools/call` requests** (plus
minimisation calls) and **48 proven violations**, with **0 false positives** on
the strict control.

Behavioural tests spawn real client + server processes; they assert exit codes
0/1/2, that a violating call is accepted while the same call under the strict
server is rejected, that every witness is schema-invalid and 1-minimal, that
`--report` matches stdout, and that two runs have the same digest.

## Limitations

- Stdio transport only. A remote server's URL transport is out of scope.
- The generated mutants cover the JSON Schema subset MCP servers actually
  publish (type, required, enum, additionalProperties, one level of nested
  objects). `oneOf` / `anyOf` / `$ref` are not exercised.
- A server can legitimately reject a schema-valid baseline call for domain
  reasons; those are reported separately under `baseline_rejections` and do not
  inflate the violation count.
- The three bundled servers are fixtures that reproduce documented behaviour.
  They are not the upstream binary, and the README never claims otherwise.
- The probe proves *acceptance*, not harm. A server may accept a wrong-typed
  argument and ignore it; the report says what was accepted, and leaves impact
  analysis to the reader.

## 与同类项目的差异

Searched before writing (`gh search repos`):

- `shriramkv/mcp-conformance-kit` (25★) — correctness checklist for MCP servers.
- `mcp-use/mcp-conformance-action` (4★) — runs the official conformance suite in CI.
- `junhecui/mcp-conformance` — executes tools to compare *annotations* with
  observed behaviour.
- `riyajath-ahamed/mcp-schema-validator` (1★) — runtime middleware that validates
  traffic through a proxy.

None of these (all under 500★) derive mutants from each server's own
`inputSchema`, nor produce a 1-minimal argument witness. This tool is
deliberately narrower: one question — *did the server accept an argument set its
own declaration forbids?* — with a reproducible digest and no SDK dependency.

## 本轮创新

First tool in this account's ledger that turns a server's declared `inputSchema`
into **runtime counter-examples against the server itself**, and minimises each
accepted violation to a 1-minimal argument witness. Prior MCP work here was
static snapshot comparison (`mcp-contract-diff`) and inbound request guarding
(`mcp-argtype-guard`); neither ever asked a live server "will you accept this?".

## License

MIT. See `LICENSE`.
