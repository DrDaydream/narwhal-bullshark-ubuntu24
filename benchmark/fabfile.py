# Copyright(C) Facebook, Inc. and its affiliates.
from fabric import task
from benchmark.adversary import local_parameters, remote_parameters
from invoke.exceptions import Exit
from benchmark.seed import SeedData

from benchmark.local import LocalBench
from benchmark.full_demo import Demo

from benchmark.logs import ParseError, LogParser
from benchmark.utils import Print, BenchError


@task(help={
    'adversary': 'Mode: none/A0, silent/A1, targeted/A3, network/A5; omitted keeps legacy behavior.',
    'header-size': 'Override the local header size in bytes.',
    'bullshark-alternating': 'Bullshark: true or false for alternate unavailable leader slots.',
})
def local(ctx, debug=True, nodes=None, workers=None, rate=None, tx_size=None,
          duration=None, faults=None, header_size=None, adversary='', byzantine=0,
          byzantine_ids='', seed=1, start_round=1, attack_rounds=0,
          delay_ms=200, max_pending_messages=4096, max_pending_bytes=67108864,
          bullshark_alternating='true'):
    ''' Run benchmarks on localhost '''
    bench_params = {
        'faults': 0,
        'nodes': 4,
        'workers': 1,
        'rate': 50_000,
        'tx_size': 512,
        'duration': 20,
    }
    node_params = {
        'header_size': 1_000,  # bytes
        'max_header_delay': '200ms',  # ms
        'gc_depth': 50,  # rounds
        'sync_retry_delay': '10_000ms',  # ms
        'sync_retry_nodes': 3,  # number of nodes
        'batch_size': 500_000,  # bytes
        'max_batch_delay': '200ms',  # ms,
        'block_synchronizer': {
            'certificates_synchronize_timeout': '2_000ms',
            'payload_synchronize_timeout': '2_000ms',
            'payload_availability_timeout': '2_000ms',
            'handler_certificate_deliver_timeout': '2_000ms'
        },
        "consensus_api_grpc": {
            "socket_addr": "/ip4/127.0.0.1/tcp/0/http",
            "get_collections_timeout": "5_000ms",
            "remove_collections_timeout": "5_000ms"
        },
        'max_concurrent_requests': 500_000
    }
    try:
        bench_params = local_parameters(
            bench_params, protocol='bullshark', nodes=nodes, workers=workers,
            rate=rate, tx_size=tx_size, duration=duration, faults=faults,
            adversary=adversary, byzantine=byzantine, byzantine_ids=byzantine_ids,
            seed=seed, start_round=start_round, attack_rounds=attack_rounds,
            delay_ms=delay_ms, max_pending_messages=max_pending_messages,
            max_pending_bytes=max_pending_bytes, bullshark_alternating=bullshark_alternating,
        )
        if header_size is not None:
            node_params['header_size'] = int(header_size)
        if node_params['header_size'] < 1:
            raise ValueError('header-size must be positive')
    except ValueError as e:
        raise Exit(str(e), code=2) from e
    try:
        ret = LocalBench(bench_params, node_params).run(debug)
        print(ret.result())
    except BenchError as e:
        Print.error(e)
        raise Exit(code=1) from e


@task
def demo(ctx, debug=True):
    ''' Run benchmarks on localhost '''
    bench_params = {
        'faults': 0,
        'nodes': 4,
        'workers': 1,
        'rate': 50_000,
        'tx_size': 512,
        'duration': 10,
    }
    node_params = {
        "batch_size": 500000,
        "block_synchronizer": {
            "certificates_synchronize_timeout": "2_000ms",
            "handler_certificate_deliver_timeout": "2_000ms",
            "payload_availability_timeout": "2_000ms",
            "payload_synchronize_timeout": "2_000ms"
        },
        "consensus_api_grpc": {
            "get_collections_timeout": "5_000ms",
            "remove_collections_timeout": "5_000ms",
            "socket_addr": "/ip4/0.0.0.0/tcp/0/http"
        },
        "gc_depth": 50,  # rounds
        "header_size": 1000,  # bytes
        "max_batch_delay": "200ms",  # ms
        "max_concurrent_requests": 500_000,
        "max_header_delay": "2000ms",  # ms
        "sync_retry_delay": "10_000ms",  # ms
        "sync_retry_nodes": 3  # number of nodes
    }
    try:
        ret = Demo(bench_params, node_params).run(debug)
        print(ret.result())
    except BenchError as e:
        Print.error(e)


@task
def seed(ctx, starting_data_port):
    ''' Run data seeder '''
    bench_params = {
        'faults': 0,
        'nodes': 4,
        'workers': 1,
        'rate': 50_000,
        'tx_size': 512,
        'duration': 20,
    }
    try:
        SeedData(bench_params).run(int(starting_data_port))
    except BenchError as e:
        Print.error(e)


@task
def create(ctx, nodes=2):
    ''' Create `nodes` new machines in EACH configured AWS region '''
    from benchmark.instance import InstanceManager
    try:
        InstanceManager.make().create_instances(nodes)
    except BenchError as e:
        Print.error(e)
        raise Exit(code=1) from e


@task
def destroy(ctx):
    ''' Destroy the testbed '''
    from benchmark.instance import InstanceManager
    try:
        InstanceManager.make().terminate_instances()
    except BenchError as e:
        Print.error(e)
        raise Exit(code=1) from e


@task
def start(ctx, max=2):
    ''' Start at most `max` machines per data center '''
    from benchmark.instance import InstanceManager
    try:
        InstanceManager.make().start_instances(max)
    except BenchError as e:
        Print.error(e)
        raise Exit(code=1) from e


@task
def stop(ctx):
    ''' Stop all machines '''
    from benchmark.instance import InstanceManager
    try:
        InstanceManager.make().stop_instances()
    except BenchError as e:
        Print.error(e)
        raise Exit(code=1) from e


@task
def info(ctx):
    ''' Display connect information about all the available machines '''
    from benchmark.instance import InstanceManager
    try:
        InstanceManager.make().print_info()
    except BenchError as e:
        Print.error(e)
        raise Exit(code=1) from e


@task
def install(ctx):
    ''' Install dependencies and compile controller/workers with live output '''
    from benchmark.remote import Bench
    try:
        Bench(ctx).install()
    except BenchError as e:
        Print.error(e)
        raise Exit(code=1) from e


@task(help={
    'adversary': 'Explicit mode: none/A0, silent/A1, targeted/A3, network/A5; omitted preserves legacy behavior.',
    'faults': 'Legacy faults (original default when mode omitted); explicit modes default to 0.',
    'delay-ms': 'Selected message delay in milliseconds, 1..86400000.',
    'byzantine': 'Fixed Byzantine count (first launch IDs), at most floor((nodes-1)/3).',
    'byzantine-ids': 'Comma-separated zero-based launch IDs; mutually exclusive with byzantine.',
    'bullshark-alternating': 'Bullshark only: true or false; alternate normal/unavailable leader slots.',
    'attack-rounds': 'Attack window length; 0 means unbounded.',
    'rate': 'Input transactions per second; comma-separated values run a sweep.',
})
def remote(ctx, debug=False, nodes=20, workers=1, rate='50000', tx_size=512,
           duration=80, faults=None, runs=1, adversary='', byzantine=0,
           byzantine_ids='', seed=1, start_round=1, attack_rounds=0,
           delay_ms=200, max_pending_messages=4096, max_pending_bytes=67108864,
           bullshark_alternating='true'):
    ''' Run a collocated AWS benchmark using binaries built by fab install '''
    from benchmark.remote import Bench
    try:
        bench_params = remote_parameters(
            nodes=nodes, workers=workers, rate=rate, tx_size=tx_size,
            duration=duration, faults=faults, runs=runs,
            default_faults=0, protocol='bullshark', adversary=adversary,
            byzantine=byzantine, byzantine_ids=byzantine_ids, seed=seed,
            start_round=start_round, attack_rounds=attack_rounds, delay_ms=delay_ms,
            max_pending_messages=max_pending_messages, max_pending_bytes=max_pending_bytes,
            bullshark_alternating=bullshark_alternating,
        )
    except ValueError as e:
        raise Exit(str(e), code=2) from e
    node_params = {
        'header_size': 1_000,  # bytes
        'max_header_delay': '200ms',  # ms
        'gc_depth': 50,  # rounds
        'sync_retry_delay': '10_000ms',  # ms
        'sync_retry_nodes': 3,  # number of nodes
        'batch_size': 500_000,  # bytes
        'max_batch_delay': '200ms',
        'block_synchronizer': {
            'certificates_synchronize_timeout': '2_000ms',
            'payload_synchronize_timeout': '2_000ms',
            'payload_availability_timeout': '2_000ms',
            'handler_certificate_deliver_timeout': '2_000ms'
        },
        "consensus_api_grpc": {
            "socket_addr": "/ip4/127.0.0.1/tcp/0/http",
            "get_collections_timeout": "5_000ms",
            "remove_collections_timeout": "5_000ms"
        },
        'max_concurrent_requests': 500_000
    }
    try:
        Bench(ctx).run(bench_params, node_params, debug)
    except BenchError as e:
        Print.error(e)
        raise Exit(code=1) from e


@task
def plot(ctx):
    ''' Plot performance using the logs generated by "fab remote" '''
    from benchmark.plot import Ploter, PlotError
    plot_params = {
        'faults': [0],
        'nodes': [10, 20, 50],
        'workers': [1],
        'collocate': True,
        'tx_size': 512,
        'max_latency': [3_500, 4_500]
    }
    try:
        Ploter.plot(plot_params)
    except PlotError as e:
        Print.error(BenchError('Failed to plot performance', e))


@task
def kill(ctx):
    ''' Stop execution on all machines '''
    from benchmark.remote import Bench
    try:
        Bench(ctx).kill()
    except BenchError as e:
        Print.error(e)
        raise Exit(code=1) from e


@task
def logs(ctx):
    ''' Print a summary of the logs '''
    try:
        print(LogParser.process('./logs', faults='?').result())
    except ParseError as e:
        Print.error(BenchError('Failed to parse logs', e))
