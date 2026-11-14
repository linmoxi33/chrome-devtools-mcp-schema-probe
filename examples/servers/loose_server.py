#!/usr/bin/env python3
"""Loose fixture: accepts every declared tool call regardless of argument types.

Reproduces the failure class documented in ChromeDevTools/chrome-devtools-mcp
issue #3455: the tool declaration says one thing, the handler casts and moves on.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mcp_base import BaseServer  # noqa: E402


class LooseServer(BaseServer):
    name = "loose-fixture"

    def validate_args(self, tool, args):
        # Deliberately no schema validation: the handler accepts the cast.
        return []


if __name__ == "__main__":
    LooseServer().serve()
