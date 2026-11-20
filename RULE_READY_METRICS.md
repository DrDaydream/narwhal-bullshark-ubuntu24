# Rule-ready latency and final E2E

Build with the `benchmark` feature to emit both latency endpoints. Toolkit v3.6
uses release-to-rule-ready for the Orca-A, Orca-B and Bullshark panels in the main
figures, and retains release-to-final in separate verification figures and CSV.
Consensus decisions, ordered output, fault selection and TPS are unchanged.

## Endpoint definitions

For a sampled transaction, on each measured replica:

```
ready latency = first batch rule-ready time - client release time
final E2E    = first final commit time      - client release time
paired wait  = final commit time           - rule-ready time
```

Release is the existing client sample event before submitting its stream.
Ingress is the worker's sample event before queueing into batching. A1 crashed
identities do not launch a client, worker or primary. The complete committee and
thresholds still include those identities; surviving clients keep their original
rate shares.

`consensus/src/readiness.rs` emits a deduplicated event for each batch digest:

```
Benchmark batch rule-ready <digest> at_us <microseconds since Unix epoch>
```

The consensus task also emits `Benchmark rule-ready instrumentation version 1`.
That marker distinguishes a run with no ready observations from old binaries
that cannot report readiness. Readiness is observational: it does not release a
transaction to the application or authorize a new consensus decision.

| Implementation | Readiness boundary |
|---|---|
| Orca-A | A rule has selected a leader and the local candidate causal sequence has been traversed, before predecessor/output gating. If previously missing history arrives later, its batches receive their own later readiness observations. |
| Orca-B | The equivalent staging point, including an ABA decision when that path needs one; before predecessor/output gating. Newly recovered history is observed when it becomes available. |
| Bullshark experiment | The current repository actually exports `tusk::Consensus`. The eligible leader and linked prior leaders have been selected. One timestamp is captured before storing the selected sequences and assigned to batches as their causal sequences are traversed. |

The final endpoint retains the existing meaning: Orca's explicit `commit_us`
before its ordered writer handoff; Tusk's existing `Committed` log timestamp
after consensus state persistence, in its output loop. Neither timestamp is an
application response or client acknowledgement. Exact execution placement is
therefore protocol-specific even though both compute `final - release`.

Readiness still includes any network delay, batching, traversal, fallback
evidence or ABA work required to reach its boundary. It excludes waiting after
that boundary. It cannot remove waiting before a leader is actually selected.
Computing final E2E as one timestamp difference never adds the same interval
twice. Equal throughput alone does not imply equal latency.

## Populations and windows

The parser joins sample IDs within their matching client/worker log pair, then
joins batch digests against each replica independently. It never takes the
earliest event across replicas. Within one replica, duplicate observations use
the earliest timestamp for the batch.

The toolkit's release cohort starts at the earliest client Start and lasts 60
seconds, with no warmup or extra drain. Each endpoint must occur by the cutoff.
Ready and final populations are counted independently. A sample ready before
the cutoff but still waiting for final contributes to ready latency and is also
reported in `ready_unfinalized_samples`. Consequently, subtracting the two
independent means is not generally the average wait.

Use these columns for a valid decomposition on the same paired population:

- `paired_ready_latency_ms`
- `paired_final_latency_ms`
- `ready_to_final_ms`

For paired observations, final minus ready equals the wait. Toolkit aggregation
first computes a mean per honest replica, then averages contributing replicas
equally. The native SUMMARY additionally reports pooled transaction/replica
distributions, so its pooled mean can differ from the toolkit mean when replica
sample counts differ. Preserve completion counts and zero-contributor markers
alongside latency; unavailable observations are not zero milliseconds.

Release, ingress, ready and final use wall-clock timestamps. Impossible ordering
is flagged and rejected by the toolkit. AWS clock synchronization remains
necessary; readiness telemetry does not eliminate cross-machine clock error.

## Files and compatibility

- Native `End-to-end latency` remains true release-to-final E2E.
- Native `RULE-READY MEASUREMENTS` lists the new endpoint and paired wait.
- Toolkit `measurement.json` has `metric=release_to_rule_ready_ms`, plus
  `final_latency_ms`, paired columns and sample counts.
- Toolkit `figures/` holds the main ready plots; `figures-final/` holds final plots.
- `measurement_stats.json` and `replicas.csv` retain sample counts and distributions.
- Mysticeti/Mahi-Mahi retain their existing shared-commit metric, explicitly
  identified in their panels. Their current logs do not provide this ready event.

Use the v3.6 updater, `fab install`, and a new result directory. The toolkit still
uses serial `create -> install -> remote -> destroy` batches per node count.
`remote` does not rebuild the binaries. Old logs without explicit readiness
cannot be used for the new native-family main metric. Plot export preserves old
final values in the verification output and marks ready as unavailable; it does
not infer readiness from final or Orca's legacy `@` field.
