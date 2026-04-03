# qlever_trapi

Convert a KGX Biolink graph to RDF for QLever, build the index, and query reified paths.

This repository now also includes a TRAPI wrapper for query-graph matching against the indexed KG.

## Setup

Use `uv run`. Do not install into the system Python or Anaconda base environment.

Create or refresh the local environment:

```bash
uv sync
```

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

## TRAPI Query Wrapper

The [qlever_trapi.py](/Users/bizon/Projects/Dogsled/qlever_trapi/qlever_trapi.py) module accepts a TRAPI request JSON document, translates an arbitrary `query_graph` topology into SPARQL, runs it against QLever, and emits a TRAPI `message` with `knowledge_graph` and `results`.

Current support:
- arbitrary qnode/qedge topology, including chains, branches, and cycles
- orphan qnodes and node-only query graphs
- empty query graphs
- qnode `ids`
- qnode `categories`
- qnode `constraints`
- qnode `set_interpretation: BATCH|ALL`
- qedge `predicates` and `predicate`
- Biolink predicate hierarchy, including canonical, inverse, symmetric, and `related_to`/any-predicate handling
- qedge `attribute_constraints`
- qedge qualifier constraints, including qualifier value hierarchy
- qedge `knowledge_type: lookup` or omitted
- bounded endpoint subclass expansion for pinned qnode `ids`
- inferred result edges plus `auxiliary_graphs` when subclass support is used
- TRAPI knowledge graph metadata projection for nodes and edges, including names, categories, attributes, qualifiers, and sources
- provenance/source reconstruction from nested `sources` resources plus downstream service attribution
- HTTP service endpoints `GET /health` and `POST /query`
- FastAPI service endpoints `GET /health`, `POST /query`, `POST /asyncquery`, `GET /asyncquery_status/{job_id}`, `GET /meta_knowledge_graph`, and `GET /metakg`

Current gap:
- `set_interpretation=MANY` is still not implemented

Example:

```bash
uv run python qlever_trapi.py --host-name localhost --port 8888 request.json
```

Disable subclass expansion:

```bash
uv run python qlever_trapi.py --subclass-depth 0 --host-name localhost --port 8888 request.json
```

Increase endpoint subclass expansion depth:

```bash
uv run python qlever_trapi.py --subclass-depth 2 --host-name localhost --port 8888 request.json
```

To run it as an HTTP service:

```bash
uv run python qlever_trapi.py --serve --listen-host 127.0.0.1 --listen-port 8000 --host-name localhost --port 8888 --subclass-depth 1
```

The HTTP service returns a JSON envelope with `status`, `description`, `http_code`, and, on success, `message`.

To run the FastAPI service:

```bash
uv run python qlever_trapi_fastapi.py --listen-host 127.0.0.1 --listen-port 8000 --host-name localhost --port 8888 --subclass-depth 1
```

The FastAPI app adds:
- `POST /asyncquery` to submit a query for background execution
- `GET /asyncquery_status/{job_id}` to poll job state and retrieve the completed TRAPI payload
- `GET /meta_knowledge_graph` and `GET /metakg` to expose a live meta knowledge graph derived from the indexed KG

### Subclass behavior

For qnodes with pinned `ids`, the wrapper can expand endpoint matches through bounded `biolink:subclass_of` support. If a query asks for `A -p-> B` and the graph only contains `A' -p-> B` with `A' subclass_of A`, the returned TRAPI result:
- binds the qnode to the queried superclass ID `A`
- returns an inferred edge in `knowledge_graph.edges`
- records the real matched edge plus the supporting subclass edge(s) in `message.auxiliary_graphs`
- annotates the inferred edge with `biolink:knowledge_level`, `biolink:agent_type`, and `biolink:support_graphs`

Set `--subclass-depth 0` to require exact endpoint ID matching with no subclass expansion.

Example request:

```json
{
  "message": {
    "query_graph": {
      "nodes": {
        "n0": {
          "ids": ["CHEBI:45783"],
          "categories": ["biolink:ChemicalEntity"]
        },
        "n1": {
          "categories": ["biolink:Disease"]
        }
      },
      "edges": {
        "e0": {
          "subject": "n0",
          "object": "n1",
          "predicates": ["biolink:treats"]
        }
      }
    }
  }
}
```

## Tests

Run:

```bash
uv run pytest -q
```
