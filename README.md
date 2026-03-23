# qlever_trapi

Convert a KGX Biolink graph to RDF for QLever, build the index, and query reified paths.

## Setup

Use `uv run`. Do not install into the system Python or Anaconda base environment.

## Layout

Generated files live under `artifacts/`:
- `artifacts/rdf/<dataset>.nt.zst`
- `artifacts/qlever/<dataset>/<dataset>*`

## Build

End-to-end build:

```bash
./build_translator_kg.sh
```

Edit the variables at the top of [build_translator_kg.sh](/Users/bizon/Projects/Dogsled/qlever_trapi/build_translator_kg.sh) for your actual input archive, dataset name, and artifact locations.

Manual conversion:

```bash
mkdir -p artifacts/rdf
uv run python kgx_to_qlever_rdf.py translator_kg.tar.zst artifacts/rdf/translator_kg.nt.zst
```

Manual indexing:

```bash
qlever index \
  --system native \
  --name artifacts/qlever/translator_kg/translator_kg \
  --format nt \
  --input-files 'artifacts/rdf/translator_kg.nt.zst' \
  --cat-input-files 'zstd -dc -- artifacts/rdf/translator_kg.nt.zst' \
  --parallel-parsing false \
  --text-index from_literals \
  --stxxl-memory 32G
```

## Start QLever

```bash
qlever start \
  --system native \
  --name artifacts/qlever/translator_kg/translator_kg \
  --memory-for-queries 16G \
  --timeout 120s
```

Use a larger timeout for large path exports; the default `30s` is too small.

## Query Paths

```bash
uv run python find_paths.py CHEBI:45783 MONDO:0004979 3 --page-size 10000000
```

With properties:

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
