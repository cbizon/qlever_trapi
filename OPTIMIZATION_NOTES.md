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
