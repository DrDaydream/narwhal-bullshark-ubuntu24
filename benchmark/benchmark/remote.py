# Copyright(C) Facebook, Inc. and its affiliates.
from collections import OrderedDict
from fabric import Connection, ThreadingGroup as Group
from fabric.exceptions import GroupException
from invoke.exceptions import Failure
from paramiko import PKey
from paramiko.ssh_exception import AuthenticationException, PasswordRequiredException, SSHException
from os.path import basename, splitext
from time import monotonic, sleep
from math import ceil
from copy import deepcopy
from itertools import zip_longest
from pathlib import Path
from shlex import quote
from os import access, X_OK
import subprocess

from benchmark.config import Committee, Key, NodeParameters, BenchParameters, ConfigError
from benchmark.utils import BenchError, Print, PathMaker, progress_bar
from benchmark.commands import CommandMaker
from benchmark.adversary import active_launch_ids, resolve_adversary, validate_adversary, write_run_snapshot
from benchmark.logs import LogParser, ParseError
from benchmark.instance import InstanceManager


class FabricError(Exception):
    ''' Wrapper for Fabric exception with a meaningfull error message. '''

    def __init__(self, error):
        assert isinstance(error, GroupException)
        failures = [f'{host.host}: {result}' for host, result in error.result.items()
                    if isinstance(result, Exception)]
        super().__init__('\n'.join(failures) or str(error))


class ExecutionError(Exception):
    pass


class Bench:
    TMUX_SOCKET = 'bullshark-benchmark'
    BUILD_MARKER = '.bullshark-build-commit'

    def __init__(self, ctx):
        self.manager = InstanceManager.make()
        self.settings = self.manager.settings
        try:
            self.connect = dict(getattr(ctx, 'connect_kwargs', {}))
            self.connect.update(
                pkey=PKey.from_path(Path(self.settings.key_path).expanduser()),
                allow_agent=False, look_for_keys=False,
                banner_timeout=30, auth_timeout=30,
            )
        except (OSError, ValueError, PasswordRequiredException, SSHException) as e:
            raise BenchError('Failed to load SSH key', e)

    def _check_stderr(self, output):
        if isinstance(output, dict):
            for x in output.values():
                if x.stderr:
                    raise ExecutionError(x.stderr)
        else:
            if output.stderr:
                raise ExecutionError(output.stderr)

    @staticmethod
    def _dependency_command():
        # Match rust-toolchain and .cargo/config. Do not change the user's
        # default Rust toolchain (the controller may also run Orca-B).
        return r'''set -euo pipefail
. /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != 24.04 ]]; then
    echo "Bullshark AWS setup requires Ubuntu 24.04" >&2
    exit 1
fi
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
packages=(build-essential gcc-12 g++-12 clang-14 libclang-14-dev cmake
          pkg-config libssl-dev zlib1g-dev protobuf-compiler tmux git curl
          ca-certificates chrony)
missing=0
for package in "${packages[@]}"; do
    if [[ "$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)" != 'install ok installed' ]]; then
        missing=1
    fi
done
if [[ "$missing" == 1 ]]; then
    sudo -n apt-get -o DPkg::Lock::Timeout=300 update
    sudo -n env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 install -y software-properties-common
    sudo -n add-apt-repository -y universe
    sudo -n apt-get -o DPkg::Lock::Timeout=300 update
    sudo -n env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 install -y --no-install-recommends "${packages[@]}"
fi
if ! command -v rustup >/dev/null 2>&1; then
    installer=$(mktemp)
    trap 'rm -f -- "$installer"' EXIT
    curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs -o "$installer"
    sh "$installer" -y --profile minimal --default-toolchain none
    rm -f -- "$installer"
    trap - EXIT
fi
if ! rustup run 1.60.0 rustc --version >/dev/null 2>&1; then
    rustup toolchain install 1.60.0 --profile minimal
fi
sudo -n systemctl enable --now chrony
chronyc waitsync 60 0.01 0.0 1
'''

    def _repository_command(self, revision=None):
        repo, url, branch = map(quote, (
            self.settings.repo_name, self.settings.repo_url, self.settings.branch,
        ))
        commands = [
            f'if [ ! -d {repo}/.git ]; then git clone --no-checkout -- {url} {repo}; fi',
            f'git -C {repo} remote set-url origin {url}',
            f'git -C {repo} fetch --force origin {branch}',
        ]
        if revision is not None:
            # A published controller commit must belong to the configured
            # branch. Every worker builds exactly this commit, even if the
            # branch advances while the testbed is being installed.
            commands.append(f'git -C {repo} merge-base --is-ancestor {quote(revision)} FETCH_HEAD')
        commands.append(f'git -C {repo} checkout --detach --force {quote(revision or "FETCH_HEAD")}')
        return ' && '.join(commands)

    @staticmethod
    def _controller_revision():
        root = subprocess.check_output(
            ['git', 'rev-parse', '--show-toplevel'],
            cwd=PathMaker.node_crate_path(), text=True,
        ).strip()
        changed = subprocess.check_output(
            ['git', 'status', '--porcelain', '--untracked-files=all', '--',
             '*.rs', '*.proto', '*.toml', 'Cargo.lock', 'rust-toolchain*', '.cargo'],
            cwd=root, text=True,
        ).strip()
        if changed:
            raise BenchError('Commit and push build-source changes, then run fab install',
                             RuntimeError(changed))
        return subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                       cwd=root, text=True).strip()

    def _wait_for_ssh(self, hosts):
        for host in hosts:
            deadline = monotonic() + 180
            while True:
                try:
                    with Connection(host, user='ubuntu', connect_kwargs=self.connect,
                                    connect_timeout=10) as connection:
                        connection.open()
                    break
                except AuthenticationException:
                    raise
                except (OSError, SSHException):
                    if monotonic() >= deadline:
                        raise
                    sleep(5)

    def install(self, hosts=None):
        hosts = self.manager.hosts(flat=True) if hosts is None else hosts
        if not hosts:
            raise BenchError('No running Bullshark instances found',
                             RuntimeError('Run fab info; create or start the testbed first'))
        Print.info('Preparing Ubuntu 24.04, Rust 1.60.0, GCC 12, Clang 14 and chrony...')
        try:
            self.revision = self._controller_revision()
            subprocess.run(['bash', '-lc', self._dependency_command()], check=True)
            self._wait_for_ssh(hosts)
            command = self._dependency_command()
            with Group(*hosts, user='ubuntu', connect_kwargs=self.connect,
                       connect_timeout=15) as group:
                group.run(f'bash -lc {quote(command)}', hide=False)
            self._update(hosts, collocate=True)
            self._build_local()
            Print.heading(f'Built commit {self.revision} on the controller and {len(hosts)} nodes; ready for fab remote')
        except (GroupException, Failure, subprocess.SubprocessError, OSError, SSHException) as e:
            e = FabricError(e) if isinstance(e, GroupException) else e
            raise BenchError('Failed to install/build testbed; fix the error and rerun fab install', e)

    def kill(self, hosts=None, delete_logs=False):
        hosts = self.manager.hosts(flat=True) if hosts is None else hosts
        assert isinstance(hosts, list)
        assert isinstance(delete_logs, bool)
        if not hosts:
            return
        delete_logs = CommandMaker.clean_logs() if delete_logs else 'true'
        cmd = [delete_logs, f'(tmux -L {self.TMUX_SOCKET} kill-server || true)']
        try:
            g = Group(*hosts, user='ubuntu', connect_kwargs=self.connect)
            g.run(' && '.join(cmd), hide=True)
        except GroupException as e:
            raise BenchError('Failed to kill nodes', FabricError(e))

    def _select_hosts(self, bench_parameters):
        # Collocate the primary and its workers on the same machine.
        if bench_parameters.collocate:
            nodes = max(bench_parameters.nodes)

            # Ensure there are enough hosts.
            hosts = self.manager.hosts()
            if sum(len(x) for x in hosts.values()) < nodes:
                return []

            # Select the hosts in different data centers.
            ordered = zip_longest(*hosts.values())
            ordered = [x for y in ordered for x in y if x is not None]
            return ordered[:nodes]

        # Spawn the primary and each worker on a different machine. Each
        # authority runs in a single data center.
        else:
            primaries = max(bench_parameters.nodes)

            # Ensure there are enough hosts.
            hosts = self.manager.hosts()
            if len(hosts.keys()) < primaries:
                return []
            for ips in hosts.values():
                if len(ips) < bench_parameters.workers + 1:
                    return []

            # Ensure the primary and its workers are in the same region.
            selected = []
            for region in list(hosts.keys())[:primaries]:
                ips = list(hosts[region])[:bench_parameters.workers + 1]
                selected.append(ips)
            return selected

    def _background_run(self, host, command, log_file):
        name = splitext(basename(log_file))[0]
        script = f'exec {command} > {quote(log_file)} 2>&1'
        cmd = (f'tmux -L {self.TMUX_SOCKET} new-session -d -s {quote(name)} '
               f'{quote("bash -lc " + quote(script))}')
        c = Connection(host, user='ubuntu', connect_kwargs=self.connect)
        output = c.run(cmd, hide=True)
        self._check_stderr(output)

    def _update(self, hosts, collocate):
        if collocate:
            ips = list(set(hosts))
        else:
            ips = list(set([x for y in hosts for x in y]))

        revision = self.revision
        Print.info(f'Building controller commit {revision} on {len(ips)} machines (live output)...')
        repo = quote(self.settings.repo_name)
        cmd = [
            self._repository_command(revision),
            f'(cd {repo}/node && bash -lc {quote(self._build_command())})',
            f'ln -sfn {repo}/target/release/node node',
            f'ln -sfn {repo}/target/release/benchmark_client benchmark_client',
        ]
        with Group(*ips, user='ubuntu', connect_kwargs=self.connect) as group:
            group.run(f'bash -lc {quote(" && ".join(cmd))}', hide=False)

    def _build_command(self):
        # Run from the node crate on either the controller or an experiment
        # host. Remove an old success stamp before attempting a rebuild.
        marker = quote(f'../target/release/{self.BUILD_MARKER}')
        return '\n'.join([
            'set -euo pipefail',
            'export PATH="$HOME/.cargo/bin:$PATH"',
            'unset ROCKSDB_LIB_DIR ROCKSDB_INCLUDE_DIR ROCKSDB_STATIC',
            f'rm -f -- {marker}',
            CommandMaker.compile(),
            'test -x ../target/release/node',
            'test -x ../target/release/benchmark_client',
            f'printf "%s\\n" {quote(self.revision)} > {marker}',
        ])

    def _build_local(self):
        Print.info(f'Building controller commit {self.revision} locally (live output)...')
        if self._controller_revision() != self.revision:
            raise BenchError('Controller commit changed during fab install',
                             RuntimeError('Rerun fab install from a stable checkout'))
        subprocess.run(['bash', '-lc', self._build_command()], check=True,
                       cwd=PathMaker.node_crate_path())
        try:
            if self._controller_revision() != self.revision:
                raise BenchError('Controller commit changed during compilation',
                                 RuntimeError('Rerun fab install from a stable checkout'))
        except (BenchError, subprocess.SubprocessError, OSError):
            Path(PathMaker.binary_path(), self.BUILD_MARKER).unlink(missing_ok=True)
            raise

    def _binary_check_command(self):
        release = quote(f'{self.settings.repo_name}/target/release')
        return ' && '.join([
            f'test "$(cat {release}/{self.BUILD_MARKER} 2>/dev/null)" = {quote(self.revision)}',
            'test -x ./node',
            'test -x ./benchmark_client',
            f'test ./node -ef {release}/node',
            f'test ./benchmark_client -ef {release}/benchmark_client',
        ])

    def _check_binaries(self, hosts):
        release = Path(PathMaker.binary_path())
        try:
            actual = (release / self.BUILD_MARKER).read_text().strip()
            if actual != self.revision:
                raise ValueError(f'Controller binaries: {actual}; expected {self.revision}')
            for name in ('node', 'benchmark_client'):
                binary = release / name
                if not binary.is_file() or not access(binary, X_OK):
                    raise FileNotFoundError(f'Missing executable: {binary}')
        except (OSError, ValueError) as e:
            raise BenchError('Controller build is missing or outdated; run fab install first', e)
        Print.info(f'Checking installed binaries for commit {self.revision} on {len(hosts)} machines...')
        try:
            with Group(*hosts, user='ubuntu', connect_kwargs=self.connect,
                       connect_timeout=15) as group:
                group.run(f'bash -lc {quote(self._binary_check_command())}', hide=True)
        except GroupException as e:
            raise BenchError('Remote build is missing, outdated or unreachable; run fab install first',
                             FabricError(e))

    def _config(self, hosts, node_parameters, bench_parameters):
        Print.info('Generating configuration files...')

        if self._controller_revision() != self.revision:
            raise BenchError('Controller commit changed during fab remote',
                             RuntimeError('Run fab install for the new commit before retrying'))

        # Cleanup all local configuration files.
        cmd = CommandMaker.cleanup()
        subprocess.run([cmd], shell=True, stderr=subprocess.DEVNULL)

        # Use the binaries compiled by fab install; this path never builds.
        cmd = CommandMaker.alias_binaries(PathMaker.binary_path())
        subprocess.run([cmd], shell=True, check=True)

        # Generate configuration files.
        keys = []
        key_files = [PathMaker.key_file(i) for i in range(len(hosts))]
        for filename in key_files:
            cmd = CommandMaker.generate_key(filename).split()
            subprocess.run(cmd, check=True)
            keys += [Key.from_file(filename)]

        names = [x.name for x in keys]

        if bench_parameters.collocate:
            workers = bench_parameters.workers
            addresses = OrderedDict(
                (x, [y] * (workers + 1)) for x, y in zip(names, hosts)
            )
        else:
            addresses = OrderedDict(
                (x, y) for x, y in zip(names, hosts)
            )
        committee = Committee(addresses, self.settings.base_port)
        committee.print(PathMaker.committee_file())

        node_parameters.print(PathMaker.parameters_file())

        # Cleanup all nodes and upload configuration files.
        names = names[:len(names)-bench_parameters.faults]
        progress = progress_bar(names, prefix='Uploading config files:')
        for i, name in enumerate(progress):
            for ip in committee.ips(name):
                c = Connection(ip, user='ubuntu', connect_kwargs=self.connect)
                c.run(f'{CommandMaker.cleanup()} || true', hide=True)
                c.put(PathMaker.committee_file(), '.')
                c.put(PathMaker.key_file(i), '.')
                c.put(PathMaker.parameters_file(), '.')

        return committee

    def _run_single(self, rate, committee, bench_parameters, debug=False,
                    adversary_config=None):
        faults = bench_parameters.faults
        active = set(active_launch_ids(committee.size(), adversary_config, faults))

        # Kill any potentially unfinished run and delete logs.
        hosts = committee.ips()
        self.kill(hosts=hosts, delete_logs=True)

        # Run the clients (they will wait for the nodes to be ready).
        # Filter all faulty nodes from the client addresses (or they will wait
        # for the faulty nodes to be online).
        Print.info('Booting clients...')
        workers_addresses = {i: rows for i, rows in enumerate(committee.workers_addresses(0))
                             if i in active}
        rate_share = ceil(rate / committee.workers())
        for i, addresses in workers_addresses.items():
            for (id, address) in addresses:
                host = Committee.ip(address)
                cmd = CommandMaker.run_client(
                    address,
                    bench_parameters.tx_size,
                    rate_share,
                    [x for y in workers_addresses.values() for _, x in y]
                )
                log_file = PathMaker.client_log_file(i, id)
                self._background_run(host, cmd, log_file)

        # Run the primaries (except the faulty ones).
        Print.info('Booting primaries...')
        for i, address in enumerate(committee.primary_addresses(0)):
            if i not in active:
                continue
            host = Committee.ip(address)
            cmd = CommandMaker.run_primary(
                PathMaker.key_file(i),
                PathMaker.committee_file(),
                PathMaker.db_path(i),
                PathMaker.parameters_file(),
                debug=debug,
                adversary_config=adversary_config,
            )
            log_file = PathMaker.primary_log_file(i)
            self._background_run(host, cmd, log_file)

        # Run the workers (except the faulty ones).
        Print.info('Booting workers...')
        for i, addresses in workers_addresses.items():
            for (id, address) in addresses:
                host = Committee.ip(address)
                cmd = CommandMaker.run_worker(
                    PathMaker.key_file(i),
                    PathMaker.committee_file(),
                    PathMaker.db_path(i, id),
                    PathMaker.parameters_file(),
                    id,  # The worker's id.
                    debug=debug
                )
                log_file = PathMaker.worker_log_file(i, id)
                self._background_run(host, cmd, log_file)

        # Wait for all transactions to be processed.
        duration = bench_parameters.duration
        for _ in progress_bar(range(20), prefix=f'Running benchmark ({duration} sec):'):
            sleep(ceil(duration / 20))
        self.kill(hosts=hosts, delete_logs=False)

    def _logs(self, committee, faults, adversary=None):
        active = set(active_launch_ids(committee.size(), adversary, faults))
        # Delete local logs (if any).
        cmd = CommandMaker.clean_logs()
        subprocess.run([cmd], shell=True, stderr=subprocess.DEVNULL)

        # Download log files.
        workers_addresses = [(i, rows) for i, rows in enumerate(committee.workers_addresses(0))
                             if i in active]
        progress = progress_bar(workers_addresses, prefix='Downloading workers logs:')
        for i, addresses in progress:
            for id, address in addresses:
                host = Committee.ip(address)
                c = Connection(host, user='ubuntu', connect_kwargs=self.connect)
                c.get(
                    PathMaker.client_log_file(i, id),
                    local=PathMaker.client_log_file(i, id)
                )
                c.get(
                    PathMaker.worker_log_file(i, id),
                    local=PathMaker.worker_log_file(i, id)
                )

        primary_addresses = [(i, address) for i, address in enumerate(committee.primary_addresses(0))
                             if i in active]
        progress = progress_bar(primary_addresses, prefix='Downloading primaries logs:')
        for i, address in progress:
            host = Committee.ip(address)
            c = Connection(host, user='ubuntu', connect_kwargs=self.connect)
            c.get(
                PathMaker.primary_log_file(i),
                local=PathMaker.primary_log_file(i)
            )

        # Parse logs and return the parser.
        Print.info('Parsing logs and computing performance...')
        return LogParser.process(PathMaker.logs_path(), faults=faults, adversary=adversary, committee_size=committee.size())

    def run(self, bench_parameters_dict, node_parameters_dict, debug=False):
        assert isinstance(debug, bool)
        Print.heading('Starting remote benchmark')
        try:
            bench_parameters = BenchParameters(bench_parameters_dict)
            node_parameters = NodeParameters(node_parameters_dict)
            adversary = validate_adversary(
                bench_parameters_dict.get('adversary'), 'bullshark',
                bench_parameters.nodes, bench_parameters.faults,
            )
        except (ConfigError, ValueError) as e:
            raise BenchError('Invalid nodes or bench parameters', e)

        # Select which hosts to use.
        selected_hosts = self._select_hosts(bench_parameters)
        if not selected_hosts:
            raise BenchError('There are not enough running Bullshark instances',
                             RuntimeError('Check fab info and the nodes/workers parameters'))

        ips = (selected_hosts if bench_parameters.collocate else
               [ip for hosts in selected_hosts for ip in hosts])
        try:
            self.revision = self._controller_revision()
        except (subprocess.SubprocessError, OSError) as e:
            raise BenchError('Failed to identify controller build sources', e)
        self._check_binaries(list(dict.fromkeys(ips)))
        Path(PathMaker.results_path()).mkdir(exist_ok=True)
        Path(PathMaker.results_path(), 'remote-commit.txt').write_text(self.revision + '\n')

        # Upload all configuration files.
        try:
            committee = self._config(
                selected_hosts, node_parameters, bench_parameters
            )
        except (subprocess.SubprocessError, GroupException, Failure, OSError, SSHException) as e:
            e = FabricError(e) if isinstance(e, GroupException) else e
            raise BenchError('Failed to configure nodes', e)

        # Run benchmarks.
        for n in bench_parameters.nodes:
            committee_copy = deepcopy(committee)
            committee_copy.remove_nodes(committee.size() - n)
            adversary_config = resolve_adversary(adversary, committee_copy.json['authorities'])

            for r in bench_parameters.rate:
                Print.heading(f'\nRunning {n} nodes (input rate: {r:,} tx/s)')

                # Run the benchmark.
                for i in range(bench_parameters.runs):
                    Print.heading(f'Run {i+1}/{bench_parameters.runs}')
                    try:
                        if adversary_config is not None:
                            snapshot = write_run_snapshot(
                                PathMaker.results_path(), bench_parameters_dict, node_parameters_dict,
                                committee_copy.json['authorities'], adversary_config,
                                n, r, i + 1, protocol='bullshark',
                            )
                            Print.info(
                                f"Adversary: {adversary_config['mode']}; fixed Byzantine IDs: "
                                f"{adversary['byzantine_ids']}; seed: {adversary_config['seed']}; "
                                f"parameters: {snapshot}"
                            )
                        self._run_single(
                            r, committee_copy, bench_parameters, debug,
                            adversary_config=adversary_config,
                        )

                        faults = bench_parameters.faults
                        logger = self._logs(committee_copy, faults, adversary=adversary)
                        logger.print(PathMaker.result_file(
                            faults,
                            n,
                            bench_parameters.workers,
                            bench_parameters.collocate,
                            r,
                            bench_parameters.tx_size,
                        ))
                    except KeyboardInterrupt:
                        self.kill(hosts=ips)
                        raise
                    except (subprocess.SubprocessError, GroupException, Failure, ParseError,
                            ExecutionError, OSError, SSHException) as e:
                        self.kill(hosts=ips)
                        if isinstance(e, GroupException):
                            e = FabricError(e)
                        raise BenchError('Benchmark failed', e)
