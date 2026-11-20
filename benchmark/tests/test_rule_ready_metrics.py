import unittest

from benchmark.measurements import Measurements, format_measurements


EPOCH = 1767225600000000
MARKER = 'Benchmark rule-ready instrumentation version 1\n'


def fixture(ready=True, second=True):
    client = '2026-01-01T00:00:00Z Start sending transactions\n'
    worker = ''
    for ident, release in [(0, 1000000), (1, 2000000)][:2 if second else 1]:
        client += f'Benchmark sample released id {ident} at_us {EPOCH + release}\n'
        worker += f'Benchmark sample ingress id {ident} at_us {EPOCH + release + 100000}\n'
        worker += f'Batch batch-{ident} contains sample tx {ident}\n'
    primary = ('2026-01-01T00:00:04.500000Z Committed B1(x) -> batch-0'
               f' @ 123 commit 456 commit_us {EPOCH + 4500000}\n')
    if ready:
        primary += MARKER
        primary += f'Benchmark batch rule-ready batch-0 at_us {EPOCH + 1500000}\n'
        if second:
            primary += f'Benchmark batch rule-ready batch-1 at_us {EPOCH + 2700000}\n'
    return client, worker, primary


class RuleReadyMetricsTests(unittest.TestCase):
    def parse(self, client, worker, primary):
        return Measurements([client], [primary], [worker]).metrics(duration=10)

    def test_readiness_does_not_wait_for_final_and_gap_uses_paired_samples(self):
        metrics = self.parse(*fixture())
        row = metrics['replicas'][0]
        self.assertEqual(metrics['rule_ready_status'], 'available')
        self.assertEqual(row['release_to_rule_ready']['count'], 2)
        self.assertEqual(row['release_to_rule_ready']['mean_ms'], 600)
        self.assertEqual(row['release_to_final']['count'], 1)
        self.assertEqual(row['release_to_final']['mean_ms'], 3500)
        self.assertEqual(row['rule_ready_to_final']['count'], 1)
        self.assertEqual(row['rule_ready_to_final']['mean_ms'], 3000)
        self.assertEqual(row['paired_release_to_rule_ready']['mean_ms'], 500)
        self.assertEqual(row['paired_release_to_final']['mean_ms'], 3500)
        self.assertEqual(row['ready_unfinalized_samples'], 1)
        self.assertIn('Release -> rule-ready', format_measurements(metrics))

    def test_old_final_and_legacy_rule_timestamp_do_not_imply_readiness(self):
        metrics = self.parse(*fixture(ready=False))
        self.assertEqual(metrics['rule_ready_status'], 'unavailable')
        self.assertIsNone(metrics['pooled']['release_to_rule_ready']['mean_ms'])
        self.assertEqual(metrics['pooled']['release_to_final']['mean_ms'], 3500)

    def test_earliest_readiness_is_used_when_batch_appears_in_multiple_sequences(self):
        client, worker, primary = fixture(second=False)
        primary += f'Benchmark batch rule-ready batch-0 at_us {EPOCH + 4000000}\n'
        metrics = self.parse(client, worker, primary)
        self.assertEqual(metrics['pooled']['release_to_rule_ready']['mean_ms'], 500)

    def test_final_before_ready_is_invalid_without_changing_final_measurement(self):
        client, worker, primary = fixture(second=False)
        primary = primary.replace(str(EPOCH + 1500000), str(EPOCH + 5000000))
        metrics = self.parse(client, worker, primary)
        self.assertEqual(metrics['replicas'][0]['invalid_rule_ready_samples'], 1)
        self.assertEqual(metrics['pooled']['release_to_final']['mean_ms'], 3500)
        self.assertIsNone(metrics['pooled']['rule_ready_to_final']['mean_ms'])

    def test_marker_distinguishes_no_ready_samples_from_old_instrumentation(self):
        client, worker, primary = fixture(ready=False)
        metrics = self.parse(client, worker, primary + MARKER)
        self.assertEqual(metrics['rule_ready_status'], 'available')
        self.assertEqual(metrics['pooled']['release_to_rule_ready']['count'], 0)

    def test_readiness_after_cutoff_is_not_counted(self):
        client, worker, primary = fixture()
        primary = primary.replace(str(EPOCH + 2700000), str(EPOCH + 11000000))
        metrics = self.parse(client, worker, primary)
        self.assertEqual(metrics['pooled']['release_to_rule_ready']['count'], 1)
        self.assertEqual(metrics['replicas'][0]['ready_unfinalized_samples'], 0)


if __name__ == '__main__':
    unittest.main()
