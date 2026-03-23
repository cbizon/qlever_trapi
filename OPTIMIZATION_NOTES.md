# Optimization Notes

Notes on possible optimizations for undirected path queries over the reified
QLever representation.

## Current Query Shape

The current path query handles undirected traversal by expanding each hop into
two possible directions:
- forward
- reverse

For a path length of `k`, this produces `2^k` `UNION` branches.

For example:
- 2 hops -> 4 branches
- 3 hops -> 8 branches
- 4 hops -> 16 branches

Each branch repeats the same structural pattern with only the
`rdf:subject`/`rdf:object` orientation swapped.

This is correct, but it has two obvious costs:
- planner and execution overhead from many union branches
- repeated evaluation of nearly identical joins

## Idea 1: Optimize The Query Shape

Keep the RDF as-is and try to reduce duplicated query work.

### Approach

Instead of building one full branch per direction combination, factor the query
into hop-local relations and join those.

For a 3-hop query, this would look like:
- first hop possibilities anchored at the start node
- middle hop possibilities between `node1` and `node2`
- last hop possibilities anchored at the end node

Conceptually:
- first hop:
  - `start -> node1`
  - `node1 -> start`
- middle hop:
  - `node1 -> node2`
  - `node2 -> node1`
- last hop:
  - `node2 -> end`
  - `end -> node2`

Then join those smaller intermediate relations instead of repeating the full
path pattern in every branch.

### Why it might help

- removes repeated `rdf:Statement` / `rdf:subject` / `rdf:object` work across
  full branches
- gives QLever narrower intermediates to join
- keeps the graph model unchanged

### Risks / limits

- QLever's optimizer may already handle parts of this well
- if the engine already factors these scans internally, gains may be small
- this likely improves planning and some repeated scans, but may not produce a
  dramatic speedup by itself

### What to benchmark

- total query time
- memory usage
- runtime/plan tree
- path count correctness
- behavior at 2-hop, 3-hop, and 4-hop path lengths

## Idea 2: Add Inverse Traversal Edges

Materialize reverse traversal support in the RDF so undirected search does not
have to be expressed as a union over stored edge direction.

### Basic idea

For each original edge, add a lightweight inverse traversal representation that
points back to the original edge.

The key goal is:
- preserve the original assertion direction for semantics and provenance
- support reverse traversal without doubling full semantic content

### Why it might help

- avoids `2^k` union growth for undirected traversal
- should simplify the path query substantially
- likely helps more as path length increases

### Costs

- increases RDF size
- increases index size and build time
- adds complexity to conversion logic
- requires care to avoid confusing traversal-only structure with "real"
  semantic assertions

### Important caution

Do not add full inverse semantic assertions as if they were equivalent new
knowledge graph edges unless that is actually intended.

If reverse support is added, it should ideally be represented as traversal
structure, not as an unqualified claim that the inverse assertion is present in
the source KGX graph.

## Idea 3: Add A Dedicated Traversal Projection

This is a refinement of the inverse-edge idea and is probably cleaner.

Keep the current reified RDF for semantics and provenance, but add a separate
path-optimized adjacency layer specifically for traversal.

### Example shapes

Possible forms include:
- `?node ql:incidentEdge ?edge`
- `?edge ql:adjacentNode ?otherNode`

or a similar pair of predicates that make undirected traversal direct and
index-friendly.

Another option is a compact node-to-node adjacency relation that still records
which original edge supports the traversal.

### Why it might be better than full inverse edges

- path queries become much simpler
- avoids semantic ambiguity
- preserves a clean separation between:
  - the knowledge representation
  - the traversal index

### Tradeoff

This still increases graph and index size, but it does so in a way that is
purpose-built for path search rather than duplicating semantic content.

## Recommended Order

### 1. Try query-shape optimization first

This is the lowest-risk change:
- no schema changes
- no converter changes
- easy to benchmark

If improvement is small, that is useful information.

### 2. If path search is a core workload, add a traversal projection

If multi-hop path queries are central to the application, a dedicated
path-optimized adjacency layer is likely the best long-term solution.

### 3. Avoid full semantic inverse duplication unless there is a strong reason

That approach is more expensive and muddies the model compared with an explicit
traversal layer.

## Working Recommendation

Short term:
- refactor the query shape and benchmark it carefully

Medium term:
- add a dedicated traversal projection during RDF conversion if path search
  performance remains a major concern

This preserves the reified model while giving QLever a structure that is much
closer to what multi-hop path expansion actually needs.
