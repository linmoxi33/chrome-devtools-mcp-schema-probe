#!/usr/bin/env python3
"""Behavioral tests for mcp-schema-probe.

Every test that talks to a server spawns a real stdio child process and speaks
JSON-RPC to it; nothing is mocked.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin", "mcp_schema_probe.py")
SERVERS = os.path.join(ROOT, "examples", "servers")
sys.path.insert(0, os.path.join(ROOT, "bin"))

import mcp_schema_probe as probe_mod  # noqa: E402


def server_cmd(name):
    return "%s %s" % (sys.executable, os.path.join(SERVERS, name))


def run_probe(server, *extra):
    cmd = [sys.executable, BIN, "probe", "--server", server, "--json"] + list(extra)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return proc


class ExitCodeTests(unittest.TestCase):
    def test_strict_server_exits_zero(self):
        proc = run_probe(server_cmd("strict_server.py"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["violations"], [])
        self.assertEqual(report["tools"], 8)
        self.assertEqual(report["baseline_rejections"], [])

    def test_loose_server_exits_one(self):
        proc = run_probe(server_cmd("loose_server.py"))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertGreater(len(report["violations"]), 20)

    def test_unknown_server_command_exits_two(self):
        proc = run_probe("/nonexistent/definitely-not-a-server")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("cannot start server", proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_empty_server_exits_two(self):
        cmd = [sys.executable, BIN, "probe", "--server", "   ", "--json"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 2)


class DetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = run_probe(server_cmd("loose_server.py"))
        cls.loose = json.loads(proc.stdout)
        proc = run_probe(server_cmd("coercing_server.py"))
        cls.coercing = json.loads(proc.stdout)

    def test_all_four_violation_kinds_present(self):
        kinds = self.loose["violations_by_kind"]
        for kind in ("missing_required", "wrong_type", "invalid_enum",
                     "unexpected_property"):
            self.assertGreaterEqual(kinds.get(kind, 0), 1, kinds)

    def test_coercing_server_detected_on_type_mismatch(self):
        kinds = self.coercing["violations_by_kind"]
        self.assertGreaterEqual(kinds.get("wrong_type", 0), 1)
        self.assertGreaterEqual(len(self.coercing["violations"]), 10)

    def test_every_witness_is_schema_invalid_and_accepted(self):
        for v in self.loose["violations"] + self.coercing["violations"]:
            self.assertTrue(v["witness_violation"], v)
            self.assertTrue(v["response_excerpt"].endswith("ok"), v)

    def test_witness_is_one_minimal(self):
        for v in self.loose["violations"] + self.coercing["violations"]:
            self.assertTrue(v["minimal_1"], v)

    def test_witnesses_are_smaller_than_valid_baselines(self):
        # A minimized witness must drop optional arguments instead of echoing
        # the full argument set the probe started from.
        loose_by_tool = {}
        for v in self.loose["violations"]:
            loose_by_tool.setdefault(v["tool"], []).append(v["witness"])
        self.assertTrue(any(len(w) <= 2 for ws in loose_by_tool.values() for w in ws))

    def test_strict_control_has_zero_false_positives(self):
        proc = run_probe(server_cmd("strict_server.py"))
        report = json.loads(proc.stdout)
        self.assertEqual(len(report["violations"]), 0)
        self.assertEqual(report["counts"]["accepted"], report["tools"])


class DeterminismTests(unittest.TestCase):
    def test_two_runs_produce_the_same_digest(self):
        a = json.loads(run_probe(server_cmd("loose_server.py")).stdout)
        b = json.loads(run_probe(server_cmd("loose_server.py")).stdout)
        self.assertEqual(a["digest"], b["digest"])
        self.assertEqual(len(a["digest"]), 64)

    def test_report_file_matches_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "report.json")
            proc = run_probe(server_cmd("loose_server.py"), "--report", path)
            self.assertEqual(proc.returncode, 1)
            with open(path) as fh:
                from_file = json.load(fh)
            self.assertEqual(from_file["digest"], json.loads(proc.stdout)["digest"])

    def test_max_mutants_caps_probe_count(self):
        full = json.loads(run_probe(server_cmd("loose_server.py")).stdout)
        capped = json.loads(run_probe(server_cmd("loose_server.py"),
                                      "--max-mutants", "2").stdout)
        self.assertLess(capped["calls"], full["calls"])
        self.assertLessEqual(sum(capped["violations_by_kind"].values()),
                             2 * capped["tools"])


class UnitTests(unittest.TestCase):
    def test_local_validator_accepts_declared_shape(self):
        tool_schema = {
            "type": "object",
            "properties": {"width": {"type": "integer"}},
            "required": ["width"],
            "additionalProperties": False,
        }
        self.assertEqual(probe_mod.schema_errors(tool_schema, {"width": 800}), [])
        self.assertTrue(probe_mod.schema_errors(tool_schema, {"width": "800"}))
        self.assertTrue(probe_mod.schema_errors(tool_schema, {}))
        self.assertTrue(probe_mod.schema_errors(tool_schema,
                                                {"width": 1, "x": 2}))

    def test_type_matches_integer_and_number(self):
        self.assertTrue(probe_mod.type_matches(5, "integer"))
        self.assertTrue(probe_mod.type_matches(5.0, "number"))
        self.assertFalse(probe_mod.type_matches(True, "integer"))
        self.assertFalse(probe_mod.type_matches("5", "integer"))

    def test_minimize_drops_redundant_arguments(self):
        class AlwaysAccept:
            rpc_messages = 0

            def request(self, method, params):
                return {"result": {"content": [{"type": "text", "text": "ok"}]}}

        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "string"},
                           "c": {"type": "string"}},
            "required": ["a"],
            "additionalProperties": False,
        }
        witness, minimal, steps = probe_mod.minimize(
            AlwaysAccept(), "t", schema,
            {"a": "x", "b": "y", "c": "z", "junk": 1}, locked=["junk"])
        self.assertEqual(witness, {"junk": 1})
        self.assertTrue(minimal)
        self.assertGreater(steps, 0)

        # Dropping the locked key would make the call schema-valid, so the
        # witness must keep it.
        tool = {"name": "t", "inputSchema": schema}
        kinds = [m["kind"] for m in probe_mod.generate_mutants(tool)]
        self.assertIn("wrong_type", kinds)
        self.assertIn("unexpected_property", kinds)

    def test_generate_mutants_is_deterministic(self):
        tool = {
            "name": "t",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["a", "b"]},
                    "count": {"type": "integer"},
                },
                "required": ["count"],
                "additionalProperties": False,
            },
        }
        first = probe_mod.generate_mutants(tool)
        second = probe_mod.generate_mutants(tool)
        self.assertEqual([m["kind"] for m in first], [m["kind"] for m in second])
        self.assertEqual(len(first), len(second))


if __name__ == "__main__":
    unittest.main(verbosity=2)
