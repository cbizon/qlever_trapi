# qlever_trapi

Tools for converting a KGX Biolink knowledge graph to RDF for QLever, building a
QLever index, and querying reified paths.

## Setup

Use the project virtual environment via `uv run`. Do not install into the
system Python or Anaconda base environment.

## Convert KGX To RDF

Convert a KGX `tar.zst` archive to N-Triples compressed with Zstandard:

```bash
uv run python kgx_to_qlever_rdf.py translator_kg.tar.zst translator_kg.nt.zst
```

What the converter does:
- streams the input archive without unpacking it first
- emits RDF N-Triples suitable for QLever
- reifies every edge as an `rdf:Statement`
- emits Biolink class, predicate, and qualifier hierarchies
- attaches nodes to their most specific Biolink classes

## Build The QLever Index

Run the end-to-end build script:

```bash
./build_translator_kg.sh
```

Or run the indexing step directly:

```bash
qlever index \
  --system native \
  --name translator_kg \
  --format nt \
  --input-files 'translator_kg.nt.zst' \
  --cat-input-files 'zstd -dc -- translator_kg.nt.zst' \
  --parallel-parsing false \
  --text-index from_literals \
  --stxxl-memory 32G
```

## Start QLever

For large path queries, increase both query memory and timeout:

```bash
qlever start \
  --system native \
  --name translator_kg \
  --memory-for-queries 16G \
  --timeout 120s
```

The default `30s` timeout is too small for very large path exports.

## Query Paths

`find_paths.py` queries the live QLever server and returns JSON.

Example:

```bash
uv run python find_paths.py CHEBI:45783 MONDO:0004979 3 --page-size 10000000
```

Include node and edge properties:

```bash
uv run python find_paths.py CHEBI:45783 MONDO:0004979 3 --page-size 10000000 --include-properties
```

Notes:
- `path_length` is counted in original graph hops, not reification hops
- path traversal is undirected with respect to stored edge orientation
- large page sizes require a sufficiently large QLever `--timeout`
- the script fails on malformed/truncated TSV pages instead of silently returning partial results

## Tests

Run:

```bash
uv run pytest -q
```
