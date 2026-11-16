"""Regression tests for the Ubuntu local benchmark adapter."""
import importlib
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from invoke import Context
from invoke.exceptions import Exit

from benchmark.local import LocalBench
from benchmark.utils import BenchError


class LocalCompatibilityTests(unittest.TestCase):
    def load_fabfile(self):
        try:
            return importlib.import_module("fabfile")
        except ImportError as error:
            self.fail(f"Local tasks must load with only local requirements: {error}")

    def test_local_task_loads_without_aws_or_plotting_packages(self):
        result = subprocess.run(
            [sys.executable, "-c", "import fabfile, sys; assert 'boto3' not in sys.modules; assert 'matplotlib' not in sys.modules"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_benchmark_returns_nonzero_exit(self):
        fabfile = self.load_fabfile()
        with patch.object(fabfile.LocalBench, "run", side_effect=BenchError("build failed", RuntimeError("compiler failed"))):
            with self.assertRaises(Exit) as caught:
                fabfile.local.body(Context())
        self.assertEqual(caught.exception.code, 1)

    def test_invalid_inputs_fail_before_starting_processes(self):
        fabfile = self.load_fabfile()
        for params in ({"nodes": 1}, {"workers": 0}, {"duration": 0}, {"rate": -1}, {"tx_size": 8}, {"faults": -1}, {"faults": 4}):
            with self.subTest(params=params):
                with patch.object(fabfile.LocalBench, "run", side_effect=AssertionError("invalid input reached process startup")):
                    with self.assertRaises(Exit):
                        fabfile.local.body(Context(), **params)

    def test_tmux_start_and_stop_use_a_dedicated_server(self):
        bench = object.__new__(LocalBench)
        with patch("benchmark.local.subprocess.run") as run:
            bench._background_run("./node run", "logs/primary-0.log")
            bench._kill_nodes()
        start, stop = [call.args[0] for call in run.call_args_list]
        self.assertEqual(start[:2], ["tmux", "-L"])
        self.assertEqual(stop[:3], start[:3])
        self.assertEqual(stop[3:], ["kill-server"])

    def test_log_redirection_quotes_paths_with_spaces(self):
        import shlex
        bench = object.__new__(LocalBench)
        with patch("benchmark.local.subprocess.run") as run:
            bench._background_run("./node run", "logs/primary with space.log")
        command = run.call_args.args[0][-1]
        self.assertEqual(shlex.split(command), ["./node", "run", ">", "logs/primary with space.log", "2>&1"])

    def test_interrupt_always_stops_local_processes(self):
        bench = object.__new__(LocalBench)
        bench.bench_parameters = type("Parameters", (), {"nodes": [4], "rate": [1000]})()
        with patch.object(bench, "_kill_nodes") as kill, patch("benchmark.local.subprocess.run", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                bench.run()
        self.assertEqual(kill.call_count, 2)

    def test_rust_build_failure_preserves_the_cause(self):
        bench = object.__new__(LocalBench)
        bench.bench_parameters = type("Parameters", (), {"nodes": [4], "rate": [1000]})()
        failure = subprocess.CalledProcessError(101, "cargo build")
        with patch.object(bench, "_kill_nodes") as kill, patch("benchmark.local.sleep"), patch("benchmark.local.subprocess.run", side_effect=[None, failure]):
            with self.assertRaises(BenchError) as caught:
                bench.run()
        self.assertIs(caught.exception.cause, failure)
        self.assertEqual(kill.call_count, 2)


if __name__ == "__main__":
    unittest.main()
