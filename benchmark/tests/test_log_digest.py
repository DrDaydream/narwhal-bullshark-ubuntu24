"""Batch byte counts and samples must use the same digest as primary commits."""
import unittest

from benchmark.logs import LogParser


class WorkerDigestTests(unittest.TestCase):
    def test_plain_digest_is_preserved(self):
        parser = object.__new__(LogParser)
        log = (
            "2026-11-16T00:00:00Z INFO worker: booted on /ip4/127.0.0.1/tcp/3003/http\n"
            "2026-11-16T00:00:00Z INFO worker: Batch Ab/c+123= contains 1024 B\n"
            "2026-11-16T00:00:00Z INFO worker: Batch Ab/c+123= contains sample tx 7\n"
        )
        sizes, samples, _ = parser._parse_workers(log)
        self.assertEqual(sizes, {"Ab/c+123=": 1024})
        self.assertEqual(samples, {7: "Ab/c+123="})

    def test_result_wrapper_is_removed_from_byte_counts(self):
        parser = object.__new__(LogParser)
        log = (
            "2026-11-16T00:00:00Z INFO worker: booted on /ip4/127.0.0.1/tcp/3003/http\n"
            "2026-11-16T00:00:00Z INFO worker: Batch Ok(Ab/c+123=) contains 1024 B\n"
        )
        sizes, _, _ = parser._parse_workers(log)
        self.assertEqual(sizes, {"Ab/c+123=": 1024})

    def test_result_wrapper_is_removed_from_transaction_samples(self):
        parser = object.__new__(LogParser)
        log = (
            "2026-11-16T00:00:00Z INFO worker: booted on /ip4/127.0.0.1/tcp/3003/http\n"
            "2026-11-16T00:00:00Z INFO worker: Batch Ok(Ab/c+123=) contains sample tx 7\n"
        )
        _, samples, _ = parser._parse_workers(log)
        self.assertEqual(samples, {7: "Ab/c+123="})


if __name__ == "__main__":
    unittest.main()
