"""AWS regression tests: no AWS credentials, SSH servers or paid resources."""
from collections import OrderedDict
from pathlib import Path
import shutil
import subprocess
import tempfile
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    import boto3
except ImportError:
    raise unittest.SkipTest('AWS tests require benchmark/requirements.txt')
from botocore.stub import Stubber
from invoke import Context
from invoke.exceptions import Exit

from benchmark.config import BenchParameters, Committee, NodeParameters
from benchmark.instance import InstanceManager
from benchmark.remote import Bench, FabricError
from benchmark.utils import BenchError


class AddressTests(unittest.TestCase):
    def test_ssh_hosts_from_committee_urls_and_multiaddrs(self):
        committee = Committee(OrderedDict([
            ('alice', ['203.0.113.10', '203.0.113.11']),
            ('bob', ['198.51.100.10', '198.51.100.11']),
        ]), 3000)
        self.assertCountEqual(committee.ips('alice'), ['203.0.113.10', '203.0.113.11'])
        self.assertEqual([Committee.ip(a) for a in committee.primary_addresses()],
                         ['203.0.113.10', '198.51.100.10'])
        self.assertEqual([Committee.ip(a) for w in committee.workers_addresses() for _, a in w],
                         ['203.0.113.11', '198.51.100.11'])


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.client = boto3.client('ec2', region_name='us-east-1',
                                  aws_access_key_id='test', aws_secret_access_key='test')
        self.manager = object.__new__(InstanceManager)
        self.manager.clients = OrderedDict([('us-east-1', self.client)])

    def test_selects_latest_canonical_noble_image(self):
        expected = {
            'Owners': ['099720109477'],
            'Filters': [
                {'Name': 'name', 'Values': ['ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*']},
                {'Name': 'state', 'Values': ['available']},
                {'Name': 'architecture', 'Values': ['x86_64']},
                {'Name': 'root-device-type', 'Values': ['ebs']},
                {'Name': 'virtualization-type', 'Values': ['hvm']},
            ],
        }
        with Stubber(self.client) as stub:
            stub.add_response('describe_images', {'Images': [
                {'ImageId': 'ami-old', 'CreationDate': '2026-01-01T00:00:00.000Z'},
                {'ImageId': 'ami-new', 'CreationDate': '2026-11-01T00:00:00.000Z'},
            ]}, expected)
            self.assertEqual(self.manager._get_ami(self.client), 'ami-new')

    def test_no_matching_image_is_an_actionable_error(self):
        with Stubber(self.client) as stub:
            stub.add_response('describe_images', {'Images': []})
            with self.assertRaises(BenchError):
                self.manager._get_ami(self.client)

    def test_discovers_all_pages_without_selecting_orca_instances(self):
        filters = [
            {'Name': 'tag:Name', 'Values': ['bullshark-node']},
            {'Name': 'instance-state-name', 'Values': ['running']},
        ]
        with Stubber(self.client) as stub:
            stub.add_response('describe_instances', {
                'Reservations': [{'Instances': [{'InstanceId': 'i-one', 'PublicIpAddress': '203.0.113.1'}]}],
                'NextToken': 'more',
            }, {'Filters': filters})
            stub.add_response('describe_instances', {
                'Reservations': [{'Instances': [{'InstanceId': 'i-two', 'PublicIpAddress': '203.0.113.2'}]}],
            }, {'Filters': filters, 'NextToken': 'more'})
            self.assertEqual(self.manager.hosts(flat=True), ['203.0.113.1', '203.0.113.2'])


class RemoteTests(unittest.TestCase):
    def test_launch_command_failure_cleans_up_already_started_clients(self):
        import fabfile
        from invoke import Result
        from invoke.exceptions import UnexpectedExit
        hosts = ['203.0.113.1', '203.0.113.2']
        committee = Committee(OrderedDict((str(i), [host, host])
                                         for i, host in enumerate(hosts)), 3000)
        events = []
        def background(host, command, log):
            events.append('launch')
            if events.count('launch') == 2:
                raise UnexpectedExit(Result(command=command, exited=1, stderr='tmux failed'))
        with patch.object(Bench, '__init__', return_value=None), \
             patch.object(Bench, '_select_hosts', return_value=hosts), \
             patch.object(Bench, '_controller_revision', return_value='a' * 40), \
             patch.object(Bench, 'install'), patch.object(Bench, '_update'), \
             patch.object(Bench, '_config', return_value=committee), \
             patch.object(Bench, '_background_run', side_effect=background), \
             patch.object(Bench, 'kill', side_effect=lambda **kw: events.append('kill')):
            with self.assertRaises(Exit):
                fabfile.remote.body(Context(), nodes=2)
        self.assertEqual(events, ['kill', 'launch', 'launch', 'kill'])

    def test_group_error_reports_failure_even_if_last_host_succeeded(self):
        from fabric import Connection
        from fabric.exceptions import GroupException
        from fabric.group import GroupResult
        from invoke import Result
        results = GroupResult({
            Connection('203.0.113.1'): RuntimeError('compiler failed'),
            Connection('203.0.113.2'): Result(stdout='ok', exited=0),
        })
        message = str(FabricError(GroupException(results)))
        self.assertIn('compiler failed', message)
        self.assertIn('203.0.113.1', message)

    def test_uneven_regions_do_not_discard_available_hosts(self):
        bench = object.__new__(Bench)
        bench.manager = SimpleNamespace(hosts=lambda: OrderedDict([
            ('a', ['a1', 'a2', 'a3']), ('b', ['b1']), ('c', ['c1', 'c2']),
        ]))
        params = SimpleNamespace(collocate=True, nodes=[6])
        self.assertEqual(bench._select_hosts(params), ['a1', 'b1', 'c1', 'a2', 'c2', 'a3'])

    def test_remote_defaults_pass_real_config_validation(self):
        import fabfile
        captured = []
        def capture(_self, bp, np, debug):
            captured.append((BenchParameters(bp), NodeParameters(np)))
        with patch.object(Bench, '__init__', return_value=None), patch.object(Bench, 'run', capture):
            fabfile.remote.body(Context())
        bp, np = captured[0]
        self.assertEqual(bp.nodes, [20])
        self.assertEqual(bp.faults, 0)
        self.assertEqual(bp.rate, [50000])
        self.assertEqual(bp.duration, 80)
        self.assertEqual(np.json['max_batch_delay'], '200ms')

    def test_failed_remote_task_has_nonzero_exit_status(self):
        import fabfile
        failure = BenchError('remote failed', RuntimeError('build failed'))
        with patch.object(Bench, '__init__', return_value=None), patch.object(Bench, 'run', side_effect=failure):
            with self.assertRaises(Exit) as caught:
                fabfile.remote.body(Context())
        self.assertEqual(caught.exception.code, 1)

    def test_remote_installs_before_building_and_configuring(self):
        import fabfile
        events = []
        with patch.object(Bench, '__init__', return_value=None), \
             patch.object(Bench, '_select_hosts', return_value=['203.0.113.1'] * 20), \
             patch.object(Bench, 'install', side_effect=lambda **kw: events.append('install')), \
             patch.object(Bench, '_update', side_effect=lambda *a: events.append('build')), \
             patch.object(Bench, '_config', side_effect=RuntimeError('stop before launching')):
            with self.assertRaises(RuntimeError):
                fabfile.remote.body(Context())
        self.assertEqual(events, ['install', 'build'])

    def test_insufficient_hosts_is_an_error(self):
        import fabfile
        with patch.object(Bench, '__init__', return_value=None), \
             patch.object(Bench, '_select_hosts', return_value=[]):
            with self.assertRaises(Exit):
                fabfile.remote.body(Context())


class CheckoutTests(unittest.TestCase):
    def test_controller_rejects_unpublished_build_sources_but_allows_local_parameters(self):
        bench = object.__new__(Bench)
        self.assertTrue(callable(getattr(bench, '_controller_revision', None)),
                        'Building a different working tree must not masquerade as the pinned commit')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=root, text=True, stderr=subprocess.STDOUT).strip()
            git('init', '-b', 'main')
            git('config', 'user.name', 'test')
            git('config', 'user.email', 'test@example.invalid')
            (root / 'main.rs').write_text('fn main() {}')
            (root / 'benchmark').mkdir()
            (root / 'benchmark/fabfile.py').write_text('rate = 50000')
            git('add', '.')
            git('commit', '-m', 'first')
            expected = git('rev-parse', 'HEAD')
            with patch('benchmark.remote.PathMaker.node_crate_path', return_value=str(root)):
                (root / 'benchmark/fabfile.py').write_text('rate = 5000')
                self.assertEqual(bench._controller_revision(), expected)
                (root / 'main.rs').write_text('fn main() { panic!(); }')
                with self.assertRaises(BenchError):
                    bench._controller_revision()
                git('add', 'main.rs')
                with self.assertRaises(BenchError):
                    bench._controller_revision()
                git('restore', '--staged', 'main.rs')
                git('restore', 'main.rs')
                (root / 'new_module.rs').write_text('fn new_module() {}')
                with self.assertRaises(BenchError):
                    bench._controller_revision()

    def test_repeated_checkout_updates_detached_repo_and_can_pin_controller_commit(self):
        bench = object.__new__(Bench)
        self.assertTrue(callable(getattr(bench, '_repository_command', None)),
                        'Remote checkout must support explicit branches and controller commits')
        bash = shutil.which('bash')
        if os.name == 'nt' and shutil.which('git'):
            # Windows' system bash may only be an unconfigured WSL launcher.
            git_bash = Path(shutil.which('git')).resolve().parents[1] / 'bin/bash.exe'
            if git_bash.is_file():
                bash = str(git_bash)
        if not bash or not shutil.which('git'):
            self.skipTest('Bash and Git required for checkout integration test')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args, cwd=root):
                return subprocess.check_output(['git', *args], cwd=cwd, text=True, stderr=subprocess.STDOUT).strip()
            git('init', '-b', 'main', 'source')
            source = root / 'source'
            git('config', 'user.name', 'test', cwd=source)
            git('config', 'user.email', 'test@example.invalid', cwd=source)
            (source / 'version').write_text('first')
            git('add', 'version', cwd=source)
            git('commit', '-m', 'first', cwd=source)
            first = git('rev-parse', 'HEAD', cwd=source)
            bench.settings = SimpleNamespace(repo_url=source.as_posix(), repo_name='checkout with space', branch='main')
            def checkout(revision=None):
                result = subprocess.run([bash, '-c', bench._repository_command(revision)],
                                        cwd=root, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            checkout()
            self.assertEqual((root / 'checkout with space/version').read_text(), 'first')
            (source / 'version').write_text('second')
            git('commit', '-am', 'second', cwd=source)
            checkout()
            self.assertEqual((root / 'checkout with space/version').read_text(), 'second')
            checkout(first)
            self.assertEqual(git('rev-parse', 'HEAD', cwd=root / 'checkout with space'), first)


if __name__ == '__main__':
    unittest.main()
