#!/usr/bin/env python3
"""Strict fixture (clean control): rejects every schema violation with -32602.

Used to prove the probe does not invent violations: a server that enforces its
declarations must produce exactly zero findings.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mcp_base import BaseServer, validate  # noqa: E402


class StrictServer(BaseServer):
    name = "strict-fixture"

    def validate_args(self, tool, args):
        return validate(tool["inputSchema"], args)


if __name__ == "__main__":
    StrictServer().serve()
