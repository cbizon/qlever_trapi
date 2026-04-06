# Optimization Notes

Notes on optimization attempts for undirected path queries over the reified
QLever representation, and on what we learned about subclass-aware matching.

## Current Baseline

The current default query in `find_paths.py` uses explicit branch expansion for
undirected traversal.

For a path length of `k`, it emits `2^k` concrete `UNION` branches:
- 2 hops -> 4 branches
- 3 hops -> 8 branches
- 4 hops -> 16 branches

Each branch fixes the direction of every hop and binds:
- `rdf:subject`
- `rdf:predicate`
- `rdf:object`

For 3-hop full retrieval on the current graph, this explicit branch-expanded
query is still the best SPARQL-only shape we tested.

## Benchmark Baseline

On the current server and index, the real workload is full retrieval through
`find_paths.py`, not a count-only query.

For `CHEBI:45783 -> MONDO:0004979`, 3 hops, full retrieval:
- page size `100k`: about `58s`
- page size `500k`: about `55s`
- page size `1M`: about `55s`
- page size `10M`: about `55s`

Structured benchmark output:
- [find_paths_page_sizes.json](/Users/bizon/Projects/Dogsled/qlever_trapi/artifacts/benchmarks/find_paths_page_sizes.json)

Conclusion:
- page size matters a little at the low end
- once page size is large enough, total export cost is dominated by
  materialization, transfer, JSON serialization, and disk writes
- the dominant problem is not paging overhead

## SPARQL Rewrite Attempts

### Hop-local union rewrite

We replaced the explicit full-path direction branches with a query that
factored the path into hop-local forward/reverse relations and joined those.

Expected benefit:
- less duplicated query text
- fewer repeated full-branch scans

Observed result:
- it was slower, not faster

3-hop full retrieval:
- baseline explicit branches: about `55s`
- hop-local union rewrite: about `89s` to `121s`

Structured benchmark output:
- [find_paths_page_sizes_optimized.json](/Users/bizon/Projects/Dogsled/qlever_trapi/artifacts/benchmarks/find_paths_page_sizes_optimized.json)

Conclusion:
- "less repeated text" did not translate into a better QLever plan
- QLever preferred the explicit branch-expanded form

### Removing `ORDER BY`

We also tested the rewritten query without `ORDER BY`, on the theory that the
global sort might be dominating runtime.

Observed result:
- still worse than the baseline
- 3-hop full retrieval ended up around `136s` to `140s`

Structured benchmark output:
- [find_paths_page_sizes_no_order.json](/Users/bizon/Projects/Dogsled/qlever_trapi/artifacts/benchmarks/find_paths_page_sizes_no_order.json)

Conclusion:
- removing `ORDER BY` did not rescue the bad rewrite
- the optimizer/execution shape was the real problem, not just the final sort

### `VALUES` endpoint rewrite

For endpoint subclass expansion, we tried pushing endpoint candidates into the
query using `VALUES`, instead of subclass joins inside the path query.

This was semantically cleaner than the subclass-join version, and QLever got
through planning quickly, but it still OOMed in execution for the 3-hop
endpoint-expanded case.

Conclusion:
- cleaner than subclass joins
- still not viable as one big query on this dataset

## Subclass Expansion Findings

### Important semantic distinction

Subclass support in this problem is auxiliary.

For a requested path between `A` and `B`, the counted path length is the length
of the supporting core path. `subclass_of` links are supporting evidence, not
counted hops.

### Endpoint-only subclassing

We compared our staged implementation of endpoint subclass expansion against
Max/Gandalf.

Method:
- find direct `subclass_of` children of the endpoint
- run the exact path query once per endpoint candidate
- normalize signatures
- merge and deduplicate client-side

Intermediate comparison files:
- [our_endpoint_signatures.tsv](/Users/bizon/Projects/Dogsled/qlever_trapi/artifacts/compare/our_endpoint_signatures.tsv)
- [max_endpoint_signatures.tsv](/Users/bizon/Projects/Dogsled/qlever_trapi/artifacts/compare/max_endpoint_signatures.tsv)

For `CHEBI:45783 -> MONDO:0004979`, 3 hops:
- asthma (`MONDO:0004979`) has `19` direct subclasses in this graph
- imatinib (`CHEBI:45783`) has `0`

The staged endpoint-expanded result matched Max/Gandalf exactly after
normalization:
- our unique signatures: `6,320,813`
- Max unique signatures: `6,320,813`
- ours not in Max: `0`
- Max not in ours: `0`

Comparison artifacts:
- [endpoint_compare_summary.json](/Users/bizon/Projects/Dogsled/qlever_trapi/artifacts/compare/endpoint_compare_summary.json)
- [our_endpoint_not_max.tsv](/Users/bizon/Projects/Dogsled/qlever_trapi/artifacts/compare/our_endpoint_not_max.tsv)
- [max_endpoint_not_ours.tsv](/Users/bizon/Projects/Dogsled/qlever_trapi/artifacts/compare/max_endpoint_not_ours.tsv)

Conclusion:
- endpoint-only subclassing is correct and reproducible
- staged expansion plus merge is operationally viable
- it matches the known Gandalf-style result

### One-shot endpoint-only subclass SPARQL

We also tried doing the same endpoint-only subclassing in one large SPARQL
query.

Observed result:
- semantically valid
- operationally bad
- OOM even with increased server memory

The failure mode moved around depending on the rewrite:
- subclass-join version failed in `Join on ?node1`
- `VALUES` rewrite got further, then failed later in sort / row-combine stages

Conclusion:
- endpoint-only subclassing is tractable in staged form
- it is not tractable here as one monolithic SPARQL query

### Every-node subclassing

We also tried a one-shot SPARQL formulation that allowed subclass support at
every position in the path.

The first version was too permissive: it effectively allowed a doubly-lifted
cross product on each hop:
- subject-lifted
- object-lifted
- both-lifted

That was the wrong semantics and blew up the internal joins badly.

We then corrected it to one-sided lifting per hop, but even that remained too
expensive online.

Conclusion:
- every-node subclassing in one-shot SPARQL is a dud on this graph
- the search space explodes before the final constraints collapse it

## What The Failed SPARQL Attempts Tell Us

The key lesson is not just that subclasses add answers.

The key lesson is that query-time subclass expansion changes the shape of the
intermediate joins, and that is what kills QLever here.

Even when the final number of extra paths is moderate, the online subclass
query can still blow memory because:
- subclass fanout is introduced before the main join collapses
- `DISTINCT` then has to deduplicate a much larger intermediate relation

So the failure is primarily about intermediate cardinality, not final answer
size.

## Reverse Traversal Layer

Separate from subclass handling, we explored a traversal-layer idea to reduce
the undirected-query branch explosion.

The only version that made concrete sense was:
- keep the original semantic edge canonical
- add a reverse traversal alias for each original edge

The reverse traversal alias is not a reversed semantic fact. It is only a
traversal handle.

Representation:
- original edge keeps canonical:
  - `rdf:subject`
  - `rdf:predicate`
  - `rdf:object`
  - original properties
- original edge also gets:
  - `kgxtr:traversal_from`
  - `kgxtr:traversal_to`
- reverse alias gets:
  - `kgxtr:traverses <original_edge>`
  - `kgxtr:traversal_from <original_object>`
  - `kgxtr:traversal_to <original_subject>`

This solves exactly one problem:
- undirected path traversal no longer needs `2^k` direction `UNION`s

It does not solve:
- subclass expansion
- path result multiplicity
- support/provenance collapse

### Misstep in the first reverse-traversal experiment

The first reverse-traversal implementation was not the version we had decided
to test.

We had explicitly discussed two designs for reverse traversal aliases:
- copy the edge properties onto the reverse alias
- keep the reverse alias lean and point back to the original edge

The implemented version was the lean pointer design, even though the agreed
experiment was the copied-edge design.

That mattered because the benchmarked traversal query then had to do extra work
on every hop:
- `OPTIONAL` lookup of `kgxtr:traverses`
- `COALESCE(...)` to recover the canonical edge id
- additional joins to read predicate and properties from the original edge

So that benchmark was not testing the intended traversal-layer idea. It was
testing a weaker, more indirect version that introduced extra per-hop
indirection.

Implication:
- the disappointing benchmark on the first reverse-traversal build does not
  fairly evaluate the copied-edge reverse-alias design
- any further judgment on the traversal approach should be based on the copied
  version, not the pointer version

### Copied-edge traversal benchmark

We then rebuilt the traversal graph using the copied-edge design we had
actually intended to test.

In that version, the reverse traversal alias is not a pointer to the original
edge. It is itself a reified `rdf:Statement` carrying the same:
- `rdf:subject`
- `rdf:predicate`
- `rdf:object`
- edge categories
- edge attributes

with only the traversal endpoints swapped:
- original edge: `kgxtr:traversal_from = subject`, `kgxtr:traversal_to = object`
- reverse alias: `kgxtr:traversal_from = object`, `kgxtr:traversal_to = subject`

That let the traversal query project the exact same SPARQL-level output shape
as the base query:
- `?subject1 ?predicate1 ?object1`
- `?subject2 ?predicate2 ?object2`
- `?subject3 ?predicate3 ?object3`

So the comparison is now between two query implementations that emit the same
path tuples at the SPARQL level, not between different result shapes.

### Base-query correction

During this comparison we also found that the first file-backed base query
templates had regressed the plan by binding the endpoints separately instead of
keeping them inline in the reified triple patterns.

That was a query-plan problem, not a semantics problem.

After restoring inline endpoint constants in:
- [original_1hop.sparql](/Users/bizon/Projects/Dogsled/qlever_trapi/queries/original_1hop.sparql)
- [original_2hop.sparql](/Users/bizon/Projects/Dogsled/qlever_trapi/queries/original_2hop.sparql)
- [original_3hop.sparql](/Users/bizon/Projects/Dogsled/qlever_trapi/queries/original_3hop.sparql)

the base count and traversal count matched again for the main 3-hop case:
- `5,015,098` paths

That removed the earlier confusion where the base count was blowing memory for
reasons that initially looked like a `DISTINCT` issue. The evidence now points
to plan quality, not to `DISTINCT` itself.

### Repeat benchmark results

We benchmarked the cleaned 3-hop query on:
- base graph
- copied-edge reverse-traversal graph

Benchmark target:
- `CHEBI:45783 -> MONDO:0004979`
- 3 hops
- page size `10,000,000`
- identical projected columns

Per-run results and averages:
- [base_vs_traversal_3hop_repeats.tsv](/Users/bizon/Projects/Dogsled/qlever_trapi/artifacts/benchmarks/repeats/base_vs_traversal_3hop_repeats.tsv)
- [original_3hop_repeat_summary.json](/Users/bizon/Projects/Dogsled/qlever_trapi/artifacts/benchmarks/repeats/original_3hop_repeat_summary.json)
- [traversal_3hop_repeat_summary.json](/Users/bizon/Projects/Dogsled/qlever_trapi/artifacts/benchmarks/repeats/traversal_3hop_repeat_summary.json)

Average timings over 5 runs:
- base:
  - count: `15.394s`
  - retrieve: `38.496s`
  - JSON write: `35.055s`
  - end-to-end export: `73.551s`
- traversal:
  - count: `13.838s`
  - retrieve: `36.160s`
  - JSON write: `35.571s`
  - end-to-end export: `71.731s`

Important invariants across all repeat runs:
- `path_count = 5,015,098`
- `retrieved_rows = 5,015,098`
- raw TSV bytes identical: `1,961,911,275`
- final JSON bytes identical: `3,576,772,956`

Interpretation:
- traversal is somewhat faster on counting
- traversal is also somewhat faster on retrieval
- JSON materialization cost is effectively the same
- the total win is modest: about `1.82s` on a `73.55s` export, or about `2.5%`

### Storage cost of traversal materialization

The copied-edge traversal graph materially increases storage:
- RDF export:
  - base: `8.7G`
  - traversal: `13G`
- QLever index directory:
  - base: `24G`
  - traversal: `34G`

So the traversal build costs roughly:
- `+4.3G` RDF output
- `+10G` index size

## Updated Conclusion

The copied-edge reverse-traversal design does work correctly, and it can beat
the base query slightly on the cleaned 3-hop benchmark.

But the gain is small relative to the storage penalty.

On this dataset and workload, the traversal graph does not improve the base
enough to justify:
- a much larger RDF artifact
- a much larger QLever index
- a more complex build

So the current practical conclusion is:
- keep the base graph/query as the default
- treat traversal materialization as an interesting experiment, not the new
  baseline
- if we revisit traversal later, we should do it only if the workload changes
  or if a larger benchmark shows a materially bigger win

## Recommended Direction

### For subclass handling

The best current approach is:
- keep a subclass map in memory
- expand endpoint candidates outside SPARQL
- run exact path queries sequentially or in coarse batches
- merge and deduplicate client-side

This is the only approach that has been both:
- correct
- proven against Max/Gandalf
- operationally reliable on this graph

### For path-query performance

If further speedup is needed for undirected pathfinding itself, the most
promising graph upgrade is:
- materialized reverse traversal aliases

That is a separate idea from subclass handling.

### What not to keep pushing

Based on the experiments so far, there is little reason to keep investing in:
- hop-local SPARQL rewrites
- giant one-shot subclass-expanded queries
- every-node online subclass expansion in SPARQL

Those paths were all benchmarked, and they all lost.

## TRAPI Wrapper Query-Planning Tuning

### Accepted optimization pass

We also tuned the `qlever_trapi.py` TRAPI wrapper directly using the standalone
performance suite in:
- `/Users/bizon/Projects/Dogsled/trapi_performance_teseter`

The accepted changes were:
- cache Biolink descendant predicate expansions in memory
- fetch the actual predicate vocabulary from the live QLever index once and
  prune each query's `VALUES ?predicate` list to predicates that really exist
  in this graph
- remove the generated top-level `ORDER BY` from TRAPI SPARQL, since TRAPI does
  not require a stable row order and result deduplication already happens
  client-side

These changes improved the packaged performance run from:
- `8.677s` total:
  - `/Users/bizon/Projects/Dogsled/trapi_performance_teseter/results/qlever_fresh2.json`

to:
- `5.272s` total:
  - `/Users/bizon/Projects/Dogsled/trapi_performance_teseter/results/qlever_after_pass1.json`

That is about a `39%` reduction in total wall time for the packaged suite.

The biggest wins were broad zero-result planners where the old query emitted a
large predicate list that mostly did not exist in the indexed graph:
- `robokop_two_hop_BiologicalEntity_assoc`
  - `2.087s -> 0.122s`
- `robokop_two_hop_ChemicalEntity_affects`
  - `1.493s -> 0.116s`

The 3-hop Imatinib-to-asthma query improved, but remained the slowest packaged
case:
- `imatinib_to_asthma_3_hop_related_to_at_instance_level`
  - `1.481s -> 1.449s` in the packaged suite

A repeat warm run stayed in the same range:
- `5.365s` total:
  - `/Users/bizon/Projects/Dogsled/trapi_performance_teseter/results/qlever_after_pass1_warm.json`

Conclusion:
- graph-aware predicate pruning is worth keeping
- removing the generated `ORDER BY` is worth keeping
- the remaining bottleneck is no longer TRAPI response assembly
- the remaining bottleneck is mostly QLever query planning on the broadest
  multi-hop zero-result cases

### Rejected TRAPI-wrapper passes

Several follow-up passes were tested and rejected because the measured suite
results got worse:
- replace category hierarchy checks with exact `rdf:type` plus expanded
  category `VALUES`
- move qnode ID/category filters earlier in the emitted SPARQL
- remove SPARQL-side `DISTINCT`
- prune synthetic subclass support for pinned IDs by probing whether subclass
  children exist
- perform extra category-pair predicate lookups to shrink the per-edge
  predicate domain further

The subclass-child pruning pass was especially bad. It blew the packaged suite
up from:
- `5.272s` total:
  - `/Users/bizon/Projects/Dogsled/trapi_performance_teseter/results/qlever_after_pass1.json`

to:
- `533.257s` total:
  - `/Users/bizon/Projects/Dogsled/trapi_performance_teseter/results/qlever_after_pass2.json`

That pass was reverted.

### Current practical ceiling

For the current code path, additional SPARQL text reshaping is giving sharply
diminishing returns. The useful improvement was to stop asking QLever to plan
against predicates that are not in the graph. Past that point, the remaining
slow cases appear to be dominated by the inherent planning cost of broad
multi-hop query shapes on this index.

So the current practical recommendation is:
- keep the accepted graph-aware predicate pruning
- stop doing speculative TRAPI-wrapper query rewrites unless we have a very
  narrow hypothesis and a benchmark to prove it
- look for the next speedup in data/index layout or traversal representation,
  not in more SPARQL string surgery
