"""A1 archives contain only surviving authorities, including all their sources."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmark.logs import LogParser, ParseError
EPOCH_US = 1794790800000000


def line(second, message):
    return f'2026-11-16T01:00:{second:06.3f}Z INFO node: {message}\n'


def write_logs(root, finals):
    count = len(finals)
    for ident, final in enumerate(finals):
        client = ''.join(line(0, text) for text in (
            'Transactions size: 512 B', 'Transactions rate: 1000 tx/s',
            'Start sending transactions', 'Benchmark latency instrumentation version 1'))
        client += line(.5, f'Benchmark sample released id 0 at_us {EPOCH_US + 500000}')
        client += line(.5, 'Sending sample transaction 0')
        worker = line(0, 'Worker booted on /ip4/127.0.0.1/tcp/3001')
        worker += line(.6, f'Benchmark sample ingress id 0 at_us {EPOCH_US + 600000}')
        worker += line(1, f'Batch batch{ident}= contains 512 B')
        worker += line(1, f'Batch batch{ident}= contains sample tx 0')
        primary = ''.join(line(0, text) for text in (
            'Header size set to 1000 B', 'Max header delay set to 200 ms',
            'Garbage collection depth set to 50', 'Sync retry delay set to 10000 ms',
            'Sync retry nodes set to 3', 'Batch size set to 500000 B',
            'Max batch delay set to 200 ms', 'Max concurrent requests set to 500000',
            'Primary booted on /ip4/127.0.0.1/tcp/3000'))
        primary += line(.8, f'Created B1(author{ident}) -> batch{ident}=')
        if final is not None:
            for batch in range(count):
                stamp = EPOCH_US + int(final * 1000000)
                primary += line(final, f'Committed B1(author{batch}) -> batch{batch}= '
                                f'@ {stamp // 1000} commit {stamp // 1000} commit_us {stamp}')
        (root / f'client-{ident}-0.log').write_text(client)
        (root / f'worker-{ident}-0.log').write_text(worker)
        (root / f'primary-{ident}.log').write_text(primary)





class CrashPopulationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        write_logs(self.root, [4] * 10)
        for ident in (7, 8, 9):
            for name in (f'primary-{ident}.log', f'worker-{ident}-0.log', f'client-{ident}-0.log'):
                (self.root / name).unlink()
        self.options = dict(mode='silent', fault_model='crash-stop', byzantine_ids=[7, 8, 9])
        pool = patch('benchmark.logs.Pool')
        fake = pool.start()
        self.addCleanup(pool.stop)
        fake.return_value.__enter__.return_value.map.side_effect = lambda f, values: list(map(f, values))

    def parse(self):
        return LogParser.process(str(self.root), adversary=self.options, committee_size=10)

    def test_only_survivors_contribute_and_reparse_preserves_original_committee(self):
        self.parse()
        parser = LogParser.process(str(self.root))
        metrics = parser.measurement_metrics(duration=60)
        self.assertEqual(parser.committee_size, 10)
        self.assertEqual(parser.faults, 3)
        self.assertEqual(metrics['crashed_primary_ids'], [7, 8, 9])
        self.assertEqual(metrics['source_pairs'], 7)
        self.assertEqual(metrics['pooled']['release_to_final']['count'], 49)
        self.assertEqual(metrics['pooled']['release_to_final']['mean_ms'], 3500)
        self.assertEqual(sum(parser.rate), 7000)

    def test_noncontiguous_survivors_keep_original_replica_ids(self):
        write_logs(self.root, [4] * 10)
        for ident in (1, 4, 8):
            for name in (f'primary-{ident}.log', f'worker-{ident}-0.log', f'client-{ident}-0.log'):
                (self.root / name).unlink()
        self.options['byzantine_ids'] = [1, 4, 8]
        parser = self.parse()
        self.assertEqual([row['replica'] for row in parser.measurement_metrics(60)['replicas']],
                         [0, 2, 3, 5, 6, 7, 9])

    def test_missing_surviving_primary_is_an_error(self):
        (self.root / 'primary-6.log').unlink()
        with self.assertRaises(ParseError):
            self.parse()

    def test_missing_client_and_worker_pair_is_an_error(self):
        (self.root / 'worker-6-0.log').unlink()
        (self.root / 'client-6-0.log').unlink()
        with self.assertRaises(ParseError):
            self.parse()


if __name__ == '__main__':
    unittest.main()
