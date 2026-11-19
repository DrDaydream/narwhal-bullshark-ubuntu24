# Bullshark scheduling adversaries

From the controller's `benchmark/` directory, after choosing the desired existing deployment branch/commit in the normal repository workflow:

The controller's committed `HEAD` must be fetchable by the AWS machines from the deployment repository and branch configured in `settings.json`; existing remote clones must have the corresponding `origin`. Use the published `adversary-experiments` branch and set `repo.branch` to `adversary-experiments` in your existing settings. Keep your existing repository directory, credentials, and regional layout. Bullshark also checks that this commit belongs to the fetched branch's history.

```bash
fab create --nodes 4
fab install
fab remote --nodes 4 --workers 1 --rate 10000 --tx-size 512 --duration 60 --faults 0 --runs 1 --adversary none
fab remote --nodes 4 --rate 10000 --duration 60 --faults 0 --runs 1 --adversary silent --byzantine 1 --bullshark-alternating false --seed 7 --start-round 1 --attack-rounds 0
fab remote --nodes 4 --rate 10000 --duration 60 --faults 0 --runs 1 --adversary targeted --byzantine-ids 1 --bullshark-alternating true --seed 7 --delay-ms 200
fab remote --nodes 4 --rate 10000 --duration 60 --faults 0 --runs 1 --adversary network --byzantine 0 --bullshark-alternating true --seed 7 --delay-ms 200
```

`create --nodes 4` requests four machines in each configured region; `remote --nodes 4` selects a four-authority committee from the existing testbed. Only create the machines required by your chosen regional layout.

Without `--adversary`, Bullshark preserves `nodes=20`, `workers=1`, `rate=50000`, `tx_size=512`, `duration=80`, `faults=0`, and `runs=1`. Its `faults` option retains its original meaning: the final launch IDs are inactive crashed processes. Fixed Byzantine IDs cannot overlap those inactive IDs.

A1 matches the original Bullshark `faults` launch behavior. With `--nodes 10 --faults 0 --adversary A1 --byzantine 3`, IDs 7, 8 and 9 do not start primary, worker or client processes. The 10-authority committee and quorum are retained. A1 requires `--start-round 1 --attack-rounds 0`, a nonempty fault set, and `--faults 0`. It disables alternating leader injection.

A3/A5 default to strict alternating leader slots: global round 2 is normal, 4 unavailable, 6 normal, 8 unavailable. `start-round` filters this schedule without restarting parity. An unavailable slot suppresses the elected leader before signing, including an honest leader. This can add a temporary fault outside the fixed Byzantine set; no aggregate `f` bound is asserted. A normal slot permits a proposal but does not guarantee a commit. Pass `--bullshark-alternating false` to disable this extra injection. A0 also disables it.

The existing controller workflow remains `fab create`, `fab install`, then `fab remote`. Run these commands from `benchmark/` with your existing `settings.json`. For Bullshark, `fab install` prepares dependencies, fetches the configured branch, and builds the controller's exact commit both locally and on the AWS machines. `fab remote` checks the commit build stamps and uses those binaries without building. Run `fab install` after changing the deployed commit; changing only adversary parameters requires another `fab remote` while the matching builds remain available. Use `fab --help remote` to inspect the parameters.

A mode is opt-in. Omitting `--adversary` keeps the old run defaults and legacy fault behavior. Explicit `--adversary none` (alias `A0`) is the no-injection baseline. The only aliases are:

| Mode | Alias | Behavior |
| --- | --- | --- |
| `none` | `A0` | No scheduling injection. |
| `silent` | `A1` | Whole-run process crashes, matching legacy Bullshark faults: omit each selected primary, all its workers and clients. |
| `targeted` | `A3` | Fixed Byzantine senders use a deterministic, leader-dependent recipient schedule to delay valid signed evidence. |
| `network` | `A5` | The delivery schedule can also delay sends from honest authorities. An empty fixed Byzantine set is allowed. |

`targeted` is selective scheduling of existing valid messages. It does not generate conflicting signed proposals or implement an equivocation/fork attack. A2 and A4 are not implemented aliases. Scheduling changes delivery and proposal emission; signatures, certificate checks, quorum thresholds, and consensus commit rules keep their original meaning.

The common experiment flags are `--nodes`, `--workers`, `--rate`, `--tx-size`, `--duration`, and `--faults`. `remote` additionally accepts `--runs` and comma-separated rate sweeps such as `--rate 10000,50000`. `--nodes` takes one count. All new mode commands default to `faults=0` if the option is omitted.

| Adversary option | Default | Meaning |
| --- | --- | --- |
| `--byzantine` | `0` | A1 selects the last `count` launch IDs; A3/A5 select IDs `0..count-1`. |
| `--byzantine-ids` | empty | Explicit comma-separated launch IDs, e.g. `0,2`; use instead of a positive count. |
| `--seed` | `1` | Deterministic scheduling seed, unsigned 64-bit integer. |
| `--start-round` | `1` | A3/A5 first affected round; A1 requires 1. |
| `--attack-rounds` | `0` | A3/A5 window length; zero is unbounded. A1 requires 0. |
| `--delay-ms` | `200` | Delay of selected sends, from 1 through 86,400,000 ms. |
| `--max-pending-messages` | `4096` | Positive per-primary bound on admitted delayed messages, including sends still awaiting transport completion. |
| `--max-pending-bytes` | `67108864` | Positive per-primary bound on their retained serialized message bytes. |
| `--bullshark-alternating` | `true` | Bullshark A3/A5 only; accepts `true` or `false`. A1 always disables it. |

For A3/A5, the bounded window is `[start_round, start_round + attack_rounds)`; overflow is rejected. Queue bounds fit the 64-bit deployment target. Fixed Byzantine membership is selected once per committee: its size cannot exceed `f = floor((nodes - 1) / 3)`, IDs must be unique and in range, and they refer to key-generation/launch order (the ID used by `.node-<id>.json`), not a public-key sort order or a shortened display ID. A3/A5 primaries receive the corresponding full public keys; A1 uses the launch IDs to omit processes. Parameter errors are rejected before constructing the AWS manager.

Every explicit-mode run writes a timestamped `results/*-n<N>-r<R>-run<I>.parameters.json` file before launch, including benchmark parameters, node parameters, the full launch-ID-to-public-key array, and resolved runtime configuration. The controller prints its path. Keep this file with the run's logs and normal performance results. A seed is deterministic for the same committee, parameters, and protocol rounds; `fab remote` generates new keys, so the seed alone is not a promise of the same identity-dependent schedule across fresh committees.

The primary environment contains one shell-quoted `BENCH_ADVERSARY_CONFIG` JSON object with `mode`, `byzantine_authorities`, `seed`, `start_round`, `rounds`, `delay_ms`, `max_pending_messages`, `max_pending_bytes`, and `bullshark_alternating`. Fabric resolves aliases and launch IDs before passing it to Rust. A3/A5 clients and workers receive ordinary workload commands. A1 omits those belonging to crashed identities.

An admitted delayed send remains counted until transport completion/acknowledgement. Its timer does not require new incoming messages, and later consensus garbage collection cannot discard it before release. When the scheduling budget is full, or a message exceeds its byte limit, that message goes immediately through the ordinary transport. The limits bound scheduling storage, not total node memory: the original transport and consensus buffers still exist, independently of the scheduler.

For A1, log download and latency statistics include only surviving authorities and their sources. The summary records the full committee size, crash count and original crashed IDs. `logs/measurement-population.json` (schema 2) preserves this information for `fab logs`; a missing surviving primary/client/worker log is an error. Historical Orca schema-1 silence archives keep their original interpretation.

A1 keeps the legacy per-client rate `ceil(rate / (nodes * workers))`; removing clients lowers the aggregate configured offered load. For 10 nodes, 3 crashes, 1 worker and rate 10000, 7 clients each use 1000 tx/s, totaling 7000 tx/s. Parameter snapshots record `fault_model`, `active_launch_ids`, `inactive_launch_ids` and `offered_rate_tps`. This is configured load, not measured ingress.

The runtime strategy is centralized in `primary/src/adversary.rs`. Python's `benchmark/benchmark/adversary.py` validates experiment parameters, chooses A1's active launch IDs, maps IDs to keys, and saves configuration and log population metadata. Supporting transport timers and queue accounting implement delivery mechanics; they do not choose adversary strategies.

## Local runs

Run `fab local` from the same `benchmark/` directory. It builds the local binaries and launches the active authorities on the current machine; it does not use AWS or require `fab create` / `fab install`. The adversary modes, launch-ID mapping, round windows, and queue bounds are the same as for `remote`.

```bash
fab local --nodes 4 --workers 1 --rate 1000 --tx-size 512 --duration 30 --header-size 20 --faults 0 --adversary none
fab local --nodes 4 --workers 1 --rate 1000 --tx-size 512 --duration 30 --header-size 20 --faults 0 --adversary silent --byzantine 1 --bullshark-alternating false --seed 7 --start-round 1 --attack-rounds 0
fab local --nodes 4 --workers 1 --rate 1000 --tx-size 512 --duration 30 --header-size 20 --faults 0 --adversary targeted --byzantine 1 --bullshark-alternating true --seed 7 --start-round 1 --attack-rounds 0 --delay-ms 200
fab local --nodes 4 --workers 1 --rate 1000 --tx-size 512 --duration 30 --header-size 20 --faults 0 --adversary network --byzantine 0 --bullshark-alternating true --seed 7 --start-round 1 --attack-rounds 0 --delay-ms 200
```

`local` accepts one input rate and executes one run: omit `--runs` and do not pass a comma-separated rate sweep. `--header-size` overrides the local header threshold in bytes; `remote` retains its existing node parameters and does not accept this flag. Omitted workload/header options use the editable dictionaries inside `local()` in `fabfile.py`, including any local customizations. A no-argument invocation preserves the original defaults and legacy behavior.

Explicit modes also save a timestamped `results/*-local-*.parameters.json` snapshot with the generated public keys and effective configuration. Runs refresh the normal `logs/` directory; preserve logs before starting a subsequent experiment. Inspect available flags with `fab --help local`.

Offline Python validation, from `benchmark/` after installing the repository's normal Python requirements:

```bash
python3 -m unittest discover -s tests -v
```

The new tests run the real Fabric argument parser, validate the config and ID mapping, capture real primary/client launch commands, and execute the nested shell command through Bash with a fake tmux/node boundary. They do not contact AWS.
