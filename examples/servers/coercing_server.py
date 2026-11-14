#!/usr/bin/env python3
"""Coercing fixture: silently converts scalars instead of enforcing the schema.

This is the "string-typed argument" failure class: "5" becomes 5, "true"
becomes True, so an agent that sends the wrong JSON type never learns about it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mcp_base import BaseServer, TOOL_BY_NAME, validate  # noqa: E402


class CoercingServer(BaseServer):
    name = "coercing-fixture"

    def validate_args(self, tool, args):
        coerced = {}
        for key, value in args.items():
            sub = (tool["inputSchema"].get("properties") or {}).get(key) or {}
            expected = sub.get("type")
            try:
                if expected == "integer" and isinstance(value, str):
                    value = int(value)
                elif expected == "number" and isinstance(value, str):
                    value = float(value)
                elif expected == "boolean" and isinstance(value, str):
                    if value.lower() in ("true", "1"):
                        value = True
                    elif value.lower() in ("false", "0"):
                        value = False
                elif expected == "string" and isinstance(value, (int, float)):
                    value = str(value)
            except (TypeError, ValueError):
                return ["%s could not be coerced to %s" % (key, expected)]
            coerced[key] = value
        return validate(tool["inputSchema"], coerced)

    def execute(self, tool, args):
        return "%s ok" % tool


if __name__ == "__main__":
    CoercingServer().serve()
