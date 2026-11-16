# Copyright(C) Facebook, Inc. and its affiliates.
from fabric import task
from invoke.exceptions import Exit
from benchmark.seed import SeedData

from benchmark.local import LocalBench
from benchmark.full_demo import Demo

from benchmark.logs import ParseError, LogParser
from benchmark.utils import Print, BenchError


@task
def local(ctx, debug=True, nodes=4, workers=1, rate=50_000, tx_size=512, duration=20, faults=0):
    ''' Run benchmarks on localhost '''
    if nodes < 2 or workers < 1 or duration < 1 or rate < 0 or tx_size < 9:
        raise Exit('Require nodes >= 2, workers >= 1, duration >= 1, rate >= 0, tx-size >= 9.', code=2)
    if not 0 <= faults < nodes:
        raise Exit('Require 0 <= faults < nodes.', code=2)
    bench_params = {
        'faults': faults,
        'nodes': nodes,
        'workers': workers,
        'rate': rate,
        'tx_size': tx_size,
        'duration': duration,
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
    ''' Create a testbed'''
    from benchmark.instance import InstanceManager
    try:
        InstanceManager.make().create_instances(nodes)
    except BenchError as e:
        Print.error(e)


@task
def destroy(ctx):
    ''' Destroy the testbed '''
    from benchmark.instance import InstanceManager
    try:
        InstanceManager.make().terminate_instances()
    except BenchError as e:
        Print.error(e)


@task
def start(ctx, max=2):
    ''' Start at most `max` machines per data center '''
    from benchmark.instance import InstanceManager
    try:
        InstanceManager.make().start_instances(max)
    except BenchError as e:
        Print.error(e)


@task
def stop(ctx):
    ''' Stop all machines '''
    from benchmark.instance import InstanceManager
    try:
        InstanceManager.make().stop_instances()
    except BenchError as e:
        Print.error(e)


@task
def info(ctx):
    ''' Display connect information about all the available machines '''
    from benchmark.instance import InstanceManager
    try:
        InstanceManager.make().print_info()
    except BenchError as e:
        Print.error(e)


@task
def install(ctx):
    ''' Install the codebase on all machines '''
    from benchmark.remote import Bench
    try:
        Bench(ctx).install()
    except BenchError as e:
        Print.error(e)


@task
def remote(ctx, debug=False):
    ''' Run benchmarks on AWS '''
    from benchmark.remote import Bench
    bench_params = {
        'faults': 3,
        'nodes': [10],
        'workers': 1,
        'collocate': True,
        'rate': [10_000, 110_000],
        'tx_size': 512,
        'duration': 300,
        'runs': 2,
    }
    node_params = {
        'header_size': 1_000,  # bytes
        'max_header_delay': '200ms',  # ms
        'gc_depth': 50,  # rounds
        'sync_retry_delay': '10_000ms',  # ms
        'sync_retry_nodes': 3,  # number of nodes
        'batch_size': 500_000,  # bytes
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


@task
def logs(ctx):
    ''' Print a summary of the logs '''
    try:
        print(LogParser.process('./logs', faults='?').result())
    except ParseError as e:
        Print.error(BenchError('Failed to parse logs', e))
