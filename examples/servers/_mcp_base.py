#!/usr/bin/env python3
"""Minimal MCP stdio server used by the probe's examples and tests.

This is a *reference fixture*, not the upstream chrome-devtools-mcp binary.
Each concrete server (loose_server.py / coercing_server.py / strict_server.py)
shares these tool declarations but differs in how it validates arguments.

Tool names, argument names and the `extraHttpHeaders` shape mirror the public
tool declarations of ChromeDevTools/chrome-devtools-mcp so that the fixture
reproduces the documented contract gap rather than inventing one.
"""

import json
import sys

PROTOCOL_VERSION = "2025-06-18"


def _obj(props, required):
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


TOOLS = [
    {
        "name": "navigate_page",
        "description": "Navigate the selected page to a URL.",
        "inputSchema": _obj({
            "url": {"type": "string"},
            "timeout": {"type": "integer"},
            "waitUntil": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle"]},
        }, ["url"]),
    },
    {
        "name": "resize_page",
        "description": "Resize the page window to the requested dimensions.",
        "inputSchema": _obj({
            "width": {"type": "integer"},
            "height": {"type": "integer"},
            "deviceScaleFactor": {"type": "number"},
        }, ["width", "height"]),
    },
    {
        "name": "emulate",
        "description": "Emulate network conditions and extra HTTP headers.",
        "inputSchema": _obj({
            "extraHttpHeaders": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
            "networkConditions": {
                "type": "object",
                "properties": {
                    "download": {"type": "number"},
                    "upload": {"type": "number"},
                },
                "required": ["download"],
                "additionalProperties": False,
            },
        }, ["extraHttpHeaders"]),
    },
    {
        "name": "evaluate_script",
        "description": "Evaluate a JavaScript function in the page.",
        "inputSchema": _obj({
            "function": {"type": "string"},
            "args": {"type": "array", "items": {"type": "string"}},
        }, ["function"]),
    },
    {
        "name": "click",
        "description": "Click the element with the given uid.",
        "inputSchema": _obj({
            "uid": {"type": "string"},
            "dblClick": {"type": "boolean"},
        }, ["uid"]),
    },
    {
        "name": "fill_form",
        "description": "Fill a set of form elements.",
        "inputSchema": _obj({
            "elements": {"type": "array", "items": {"type": "object"}},
            "submit": {"type": "boolean"},
        }, ["elements"]),
    },
    {
        "name": "list_console_messages",
        "description": "List console messages for the selected page or a service worker.",
        "inputSchema": _obj({
            "serviceWorkerId": {"type": "string"},
            "pageSize": {"type": "integer"},
        }, []),
    },
    {
        "name": "select_page",
        "description": "Select a page by id.",
        "inputSchema": _obj({
            "pageId": {"type": "integer"},
            "bringToFront": {"type": "boolean"},
        }, ["pageId"]),
    },
]

TOOL_BY_NAME = {t["name"]: t for t in TOOLS}

JSON_TYPES = {
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
}


def validate(schema, value, path="$"):
    """Return a list of human-readable schema violations."""
    errors = []
    expected = schema.get("type")
    if expected and expected in JSON_TYPES and not JSON_TYPES[expected](value):
        return ["%s must be %s, got %s" % (path, expected, type(value).__name__)]
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
            if name in value and isinstance(sub, dict):
                errors.extend(validate(sub, value[name], "%s.%s" % (path, name)))
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            for name, item in value.items():
                if name not in props:
                    errors.extend(validate(extra, item, "%s.%s" % (path, name)))
    elif isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(value):
                errors.extend(validate(items, item, "%s[%d]" % (path, i)))
    if isinstance(schema.get("enum"), list) and value not in schema["enum"]:
        errors.append("%s must be one of %s" % (path, schema["enum"]))
    return errors


class BaseServer:
    """Speaks MCP over stdio. Subclasses decide argument policy."""

    name = "base"

    def validate_args(self, tool, args):
        return []

    def execute(self, tool, args):
        return "%s ok" % tool

    # -- transport -------------------------------------------------------
    def _reply(self, msg_id, result):
        self._write({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _error(self, msg_id, code, message):
        self._write({"jsonrpc": "2.0", "id": msg_id,
                     "error": {"code": code, "message": message}})

    def _write(self, obj):
        sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    def _handle(self, msg):
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            self._reply(msg_id, {
                "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.name, "version": "1.0.0"},
            })
        elif method == "notifications/initialized":
            return
        elif method == "ping":
            self._reply(msg_id, {})
        elif method == "tools/list":
            self._reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            tool = TOOL_BY_NAME.get(name)
            if tool is None:
                self._error(msg_id, -32602, "unknown tool %r" % name)
                return
            errors = self.validate_args(tool, args)
            if errors:
                self._error(msg_id, -32602, "; ".join(errors[:6]))
                return
            try:
                text = self.execute(name, args)
            except Exception as exc:  # domain failure, not a schema failure
                self._reply(msg_id, {"content": [{"type": "text", "text": str(exc)}],
                                     "isError": True})
                return
            self._reply(msg_id, {"content": [{"type": "text", "text": text}],
                                 "isError": False})
        else:
            if msg_id is not None:
                self._error(msg_id, -32601, "method not found: %r" % method)

    def serve(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            self._handle(msg)


if __name__ == "__main__":
    BaseServer().serve()
