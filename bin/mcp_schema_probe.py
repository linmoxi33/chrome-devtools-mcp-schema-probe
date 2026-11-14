#!/usr/bin/env python3
"""mcp-schema-probe: run a real MCP stdio server and check whether it actually
enforces the inputSchema it advertises.

Zero third-party dependencies. Python 3.8+.
"""

import argparse
import hashlib
import json
import select
import shlex
import subprocess
import sys
import time

VERSION = "1.0.0"
PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "mcp-schema-probe", "version": VERSION}


class ProbeError(Exception):
    """Usage, transport or startup failure -> exit code 2."""


class ServerError(ProbeError):
    pass


# --------------------------------------------------------------------------
# JSON-RPC over stdio
# --------------------------------------------------------------------------

class StdioClient:
    def __init__(self, argv, timeout=5.0):
        self.argv = argv
        self.timeout = timeout
        self.proc = None
        self._next_id = 1
        self.rpc_messages = 0

    def start(self):
        try:
            self.proc = subprocess.Popen(
                self.argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise ProbeError("cannot start server %r: %s" % (self.argv, exc))

    def _send(self, obj):
        line = json.dumps(obj, separators=(",", ":"))
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ServerError("server closed stdin: %s" % exc)
        self.rpc_messages += 1

    def _read(self):
        ready, _, _ = select.select([self.proc.stdout], [], [], self.timeout)
        if not ready:
            raise ServerError("timeout after %.1fs waiting for a JSON-RPC reply" % self.timeout)
        line = self.proc.stdout.readline()
        if line == "":
            err = ""
            try:
                err = (self.proc.stderr.read() or "")[-400:]
            except Exception:
                pass
            raise ServerError("server exited before replying%s" % (": " + err if err else ""))
        self.rpc_messages += 1
        try:
            return json.loads(line)
        except ValueError:
            raise ServerError("server wrote a non-JSON line: %r" % line[:200])

    def request(self, method, params=None):
        msg_id = self._next_id
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)
        # Skip server notifications / unrelated ids.
        deadline = time.time() + self.timeout * 3
        while time.time() < deadline:
            msg = self._read()
            if msg.get("id") == msg_id:
                return msg
        raise ServerError("no reply for request id %d" % msg_id)

    def notify(self, method, params=None):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

    def close(self):
        if self.proc is None:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Schema handling (the JSON Schema subset MCP servers actually publish)
# --------------------------------------------------------------------------

def json_type_of(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number" if isinstance(value, float) else "integer"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def type_matches(value, expected):
    actual = json_type_of(value)
    if expected == "integer":
        return actual == "integer" or (actual == "number" and float(value).is_integer())
    if expected == "number":
        return actual in ("integer", "number")
    if expected == "boolean":
        return actual == "boolean"
    if expected == "object":
        return actual == "object"
    if expected == "array":
        return actual == "array"
    if expected == "string":
        return actual == "string"
    return True


def schema_errors(schema, value, path="$"):
    """Independent local validator for the JSON Schema subset MCP servers publish.

    The probe must be able to say "this argument set violates the declaration"
    without asking the server under test.
    """
    errors = []
    if not isinstance(schema, dict):
        return errors
    expected = schema.get("type")
    if isinstance(expected, str) and expected in (
            "string", "boolean", "integer", "number", "object", "array", "null"):
        if expected == "null":
            if value is not None:
                errors.append("%s must be null" % path)
        elif not type_matches(value, expected):
            errors.append("%s must be %s, got %s" % (path, expected, json_type_of(value)))
            return errors
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in value:
                errors.append("%s.%s is required" % (path, name))
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in props:
                    errors.append("%s.%s is not a declared property" % (path, name))
        for name, sub in props.items():
            if name in value:
                errors.extend(schema_errors(sub, value[name], "%s.%s" % (path, name)))
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            for name, item in value.items():
                if name not in props:
                    errors.extend(schema_errors(extra, item, "%s.%s" % (path, name)))
    elif isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(value):
                errors.extend(schema_errors(items, item, "%s[%d]" % (path, i)))
    if isinstance(schema.get("enum"), list) and value not in schema["enum"]:
        errors.append("%s must be one of %s" % (path, schema["enum"]))
    return errors


def sample_value(schema):
    """A plausible, schema-valid value for a property."""
    if not isinstance(schema, dict):
        return "probe"
    if "const" in schema:
        return schema["const"]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    if "default" in schema:
        return schema["default"]
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    if t == "string":
        return "probe"
    if t == "integer":
        return 1
    if t == "number":
        return 1.0
    if t == "boolean":
        return True
    if t == "array":
        items = schema.get("items") or {}
        return [sample_value(items)] if isinstance(items, dict) and items else []
    if t == "object":
        props = schema.get("properties") or {}
        return {k: sample_value(v) for k, v in props.items()}
    return "probe"


def wrong_value(schema):
    """A value that violates the declared type of `schema`."""
    t = schema.get("type") if isinstance(schema, dict) else "string"
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    if t == "string":
        return 12345
    if t in ("integer", "number"):
        return "7"
    if t == "boolean":
        return "true"
    if t == "object":
        return [1, 2, 3]
    if t == "array":
        return {"not": "an-array"}
    return 12345


def build_base_args(schema):
    props = (schema or {}).get("properties") or {}
    return {name: sample_value(sub) for name, sub in props.items()}


def _mutant(kind, args, expectation, locked=()):
    return {"kind": kind, "arguments": args, "expectation": expectation,
            "locked": list(locked)}


def generate_mutants(tool, limit_per_tool=None):
    """Deterministic schema-violating argument sets for one tool."""
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = list(schema.get("required") or [])
    base = build_base_args(schema)
    out = []

    # 1. required property removed
    for name in required:
        if name not in base:
            continue
        args = {k: v for k, v in base.items() if k != name}
        out.append(_mutant("missing_required", args, "required property %r is absent" % name))

    # 2. property carrying the wrong JSON type
    for name, sub in props.items():
        if not isinstance(sub, dict) or "type" not in sub:
            continue
        args = dict(base)
        args[name] = wrong_value(sub)
        out.append(_mutant("wrong_type", args,
                           "%r is declared %s, sent %s" % (
                               name, sub.get("type"), json_type_of(args[name])),
                           locked=[name] + required))

    # 3. invalid enum member
    for name, sub in props.items():
        if isinstance(sub, dict) and isinstance(sub.get("enum"), list) and sub["enum"]:
            args = dict(base)
            args[name] = "__not_in_enum__"
            out.append(_mutant("invalid_enum", args,
                               "%r is constrained to enum %s" % (name, sub["enum"]),
                               locked=[name] + required))

    # 4. undeclared property when the schema forbids it
    if schema.get("additionalProperties") is False:
        args = dict(base)
        args["__probe_undeclared__"] = "x"
        out.append(_mutant("unexpected_property", args,
                           "additionalProperties is false",
                           locked=["__probe_undeclared__"] + required))

    # 5. nested required property removed (one level deep)
    for name, sub in props.items():
        if not isinstance(sub, dict) or sub.get("type") != "object":
            continue
        nested_req = list(sub.get("required") or [])
        nested_props = sub.get("properties") or {}
        for nested in nested_req:
            if name not in base or not isinstance(base[name], dict):
                continue
            inner = dict(base[name])
            inner.pop(nested, None)
            args = dict(base)
            args[name] = inner
            out.append(_mutant("nested_missing_required", args,
                               "%r.%r is required by the nested object" % (name, nested),
                               locked=[name] + required))
            break
        break

    # Deduplicate by canonical arguments + kind.
    seen = set()
    unique = []
    for m in out:
        key = (m["kind"], json.dumps(m["arguments"], sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        unique.append(m)
    if limit_per_tool:
        unique = unique[:limit_per_tool]
    return unique


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------

def call_tool(client, name, arguments):
    msg = client.request("tools/call", {"name": name, "arguments": arguments})
    if "error" in msg:
        return "rejected", msg["error"].get("message", "")
    result = msg.get("result") or {}
    if result.get("isError") is True:
        text = ""
        for part in result.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "text":
                text += part.get("text", "")
        return "rejected", text[:300]
    text = ""
    for part in result.get("content") or []:
        if isinstance(part, dict) and part.get("type") == "text":
            text += part.get("text", "")
    return "accepted", text[:300]


def is_accepted(client, name, arguments):
    try:
        verdict, _ = call_tool(client, name, arguments)
    except ServerError:
        return None
    return verdict == "accepted"


def still_violating(client, name, schema, arguments):
    """A witness only counts if it is schema-invalid *and* the server accepted it."""
    if not schema_errors(schema, arguments):
        return False
    return is_accepted(client, name, arguments) is True


def minimize(client, name, schema, arguments, locked=(), max_steps=24):
    """Greedy 1-minimal witness that keeps the same violation class.

    `locked` holds top-level argument names that define the violation (the
    wrong-typed property, the undeclared property, ...). Keys outside that set
    are dropped while the call still violates the schema and is still accepted.
    """
    current = dict(arguments)
    locked = set(locked)
    steps = 0
    changed = True
    while changed and steps < max_steps:
        changed = False
        for key in sorted(current):
            if key in locked:
                continue
            trial = {k: v for k, v in current.items() if k != key}
            steps += 1
            if still_violating(client, name, schema, trial):
                current = trial
                changed = True
                break
    minimal = True
    for key in sorted(current):
        if key in locked:
            continue
        trial = {k: v for k, v in current.items() if k != key}
        steps += 1
        if still_violating(client, name, schema, trial):
            minimal = False
            break
    return current, minimal, steps


def probe(argv, timeout=5.0, max_mutants=None):
    client = StdioClient(argv, timeout=timeout)
    started = time.time()
    calls = 0
    report = {
        "tool": "mcp-schema-probe",
        "version": VERSION,
        "server": {"command": " ".join(shlex.quote(a) for a in argv)},
        "protocol_version": None,
        "tools": 0,
        "calls": 0,
        "rpc_messages": 0,
        "counts": {"accepted": 0, "rejected": 0, "timeout": 0, "crash": 0},
        "violations": [],
        "violations_by_kind": {},
        "baseline_rejections": [],
        "elapsed_seconds": 0.0,
    }
    try:
        client.start()
        init = client.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        })
        if "error" in init:
            raise ServerError("initialize failed: %s" % init["error"])
        report["protocol_version"] = (init.get("result") or {}).get("protocolVersion")
        client.notify("notifications/initialized")

        listing = client.request("tools/list", {})
        if "error" in listing:
            raise ServerError("tools/list failed: %s" % listing["error"])
        tools = (listing.get("result") or {}).get("tools") or []
        report["tools"] = len(tools)

        for tool in tools:
            name = tool.get("name")
            if not name:
                continue
            base = build_base_args(tool.get("inputSchema") or {})
            # Baseline call: a schema-valid argument set must be accepted. A
            # rejection here is a *different* defect, so it is recorded as a
            # diagnostic rather than a violation.
            try:
                verdict, excerpt = call_tool(client, name, base)
                calls += 1
                report["counts"][verdict] += 1
                if verdict != "accepted":
                    report.setdefault("baseline_rejections", []).append(
                        {"tool": name, "arguments": base, "response": excerpt})
            except ServerError as exc:
                report["counts"]["timeout"] += 1
                report.setdefault("transport_errors", []).append(
                    {"tool": name, "error": str(exc)[:200]})
                break

            for mutant in generate_mutants(tool, limit_per_tool=max_mutants):
                try:
                    verdict, excerpt = call_tool(client, name, mutant["arguments"])
                    calls += 1
                except ServerError as exc:
                    report["counts"]["timeout"] += 1
                    report.setdefault("transport_errors", []).append(
                        {"tool": name, "error": str(exc)[:200]})
                    break
                report["counts"][verdict] += 1
                if verdict != "accepted":
                    continue
                witness, minimal, steps = minimize(
                    client, name, tool.get("inputSchema") or {},
                    mutant["arguments"], locked=mutant["locked"])
                calls += steps
                report["violations"].append({
                    "tool": name,
                    "kind": mutant["kind"],
                    "expectation": mutant["expectation"],
                    "witness": witness,
                    "witness_violation": schema_errors(
                        tool.get("inputSchema") or {}, witness)[0],
                    "minimal_1": minimal,
                    "response_excerpt": excerpt,
                })
                report["violations_by_kind"][mutant["kind"]] = \
                    report["violations_by_kind"].get(mutant["kind"], 0) + 1
    finally:
        client.close()

    report["calls"] = calls
    report["rpc_messages"] = client.rpc_messages
    report["elapsed_seconds"] = round(time.time() - started, 3)
    report["violations"].sort(key=lambda v: (v["tool"], v["kind"],
                                             json.dumps(v["witness"], sort_keys=True)))
    # The digest must be reproducible across runs: wall-clock time and the
    # digest itself are excluded.
    stable = {k: v for k, v in report.items() if k not in ("elapsed_seconds", "digest")}
    digest_payload = json.dumps(stable, sort_keys=True, separators=(",", ":"))
    report["digest"] = hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()
    return report


# --------------------------------------------------------------------------
# Human-readable output
# --------------------------------------------------------------------------

def render_text(report):
    lines = []
    lines.append("mcp-schema-probe %s" % report["version"])
    lines.append("server : %s" % report["server"]["command"])
    lines.append("tools  : %d   calls: %d   rpc messages: %d   %.2fs" % (
        report["tools"], report["calls"], report["rpc_messages"],
        report["elapsed_seconds"]))
    c = report["counts"]
    lines.append("calls  : accepted=%d rejected=%d timeout=%d crash=%d" % (
        c["accepted"], c["rejected"], c["timeout"], c["crash"]))
    lines.append("violations: %d" % len(report["violations"]))
    for v in report["violations"]:
        lines.append("  [%s] %s  witness=%s  minimal_1=%s" % (
            v["kind"], v["tool"], json.dumps(v["witness"], sort_keys=True),
            v["minimal_1"]))
        lines.append("      expected: %s" % v["expectation"])
    if report.get("baseline_rejections"):
        lines.append("baseline rejections (schema-valid call refused): %d" %
                     len(report["baseline_rejections"]))
    if report.get("transport_errors"):
        lines.append("transport errors: %d" % len(report["transport_errors"]))
    lines.append("digest : %s" % report["digest"])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="mcp-schema-probe",
        description="Run an MCP stdio server and check whether it enforces its own inputSchema.")
    parser.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("probe", help="spawn a server, probe every tool, report violations")
    p.add_argument("--server", required=True,
                   help="command line used to start the MCP stdio server, e.g. "
                        "'python3 examples/servers/strict_server.py'")
    p.add_argument("--json", action="store_true", help="print a machine-readable report")
    p.add_argument("--report", help="also write the JSON report to this path")
    p.add_argument("--timeout", type=float, default=5.0,
                   help="per-message timeout in seconds (default: 5)")
    p.add_argument("--max-mutants", type=int, default=None,
                   help="cap schema-violating probes per tool")

    args = parser.parse_args(argv)
    if args.command != "probe":
        parser.print_usage(sys.stderr)
        return 2

    try:
        argv_server = shlex.split(args.server)
    except ValueError as exc:
        sys.stderr.write("mcp-schema-probe: cannot parse --server: %s\n" % exc)
        return 2
    if not argv_server:
        sys.stderr.write("mcp-schema-probe: --server is empty\n")
        return 2

    try:
        report = probe(argv_server, timeout=args.timeout, max_mutants=args.max_mutants)
    except ProbeError as exc:
        sys.stderr.write("mcp-schema-probe: %s\n" % exc)
        return 2

    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
            fh.write("\n")

    if args.json:
        json.dump(report, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print(render_text(report))

    return 1 if report["violations"] else 0


if __name__ == "__main__":
    sys.exit(main())
