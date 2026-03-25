#!/usr/bin/env python3
import argparse
import csv
import json
import io
import sys
import time
import urllib.parse
import urllib.request
from contextlib import closing
from itertools import product
from typing import Any


RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS_NS = "http://www.w3.org/2000/01/rdf-schema#"
IDENTIFIERS_ORG = "https://identifiers.org/"
BIOLINK_VOCAB = "https://w3id.org/biolink/vocab/"
BIOLINK_SUBCLASS_OF = BIOLINK_VOCAB + "subclass_of"
REIFICATION_PREDICATES = {
    RDF_NS + "subject",
    RDF_NS + "predicate",
    RDF_NS + "object",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find undirected paths through reified KGX edges in QLever."
    )
    parser.add_argument("start_curie", help="Start CURIE, for example CHEBI:45783")
    parser.add_argument("end_curie", help="End CURIE, for example MONDO:0004979")
    parser.add_argument(
        "path_length",
        type=int,
        help="Path length in original edge count. Use 1 for direct paths, 2 for one intermediate node, etc.",
    )
    parser.add_argument(
        "--host-name",
        default="localhost",
        help="QLever host name. Default: localhost",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8888,
        help="QLever port. Default: 8888",
    )
    parser.add_argument(
        "--include-properties",
        action="store_true",
        help="Fetch additional outgoing properties for the nodes and edges in the returned paths.",
    )
    parser.add_argument(
        "--access-token",
        default=None,
        help="Optional QLever access token.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional limit on returned paths.",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=100000,
        help="Fetch paths in pages of this many rows. Default: 100000",
    )
    parser.add_argument(
        "--include-subclasses",
        action="store_true",
        help="Allow each path position to match via a single subclass_of support edge.",
    )
    return parser.parse_args()


def curie_to_iri(value: str) -> str:
    if value.startswith(("http://", "https://", "urn:")):
        return value
    if value.startswith("biolink:"):
        return BIOLINK_VOCAB + value.split(":", 1)[1]
    return IDENTIFIERS_ORG + value


def normalize_iri(value: str) -> str:
    if value.startswith("<") and value.endswith(">"):
        return value[1:-1]
    return value


def iri_term(iri: str) -> str:
    return f"<{iri}>"


def variable(name: str) -> str:
    return f"?{name}"


def middle_node_var(index: int) -> str:
    return variable(f"node{index}")


def edge_var(index: int) -> str:
    return variable(f"edge{index}")


def predicate_var(index: int) -> str:
    return variable(f"pred{index}")


def direction_var(index: int) -> str:
    return variable(f"dir{index}")


def label_var(index: int) -> str:
    return variable(f"node{index}_label")


def witness_node_var(index: int) -> str:
    return variable(f"match{index}")


def subclass_edge_var(index: int) -> str:
    return variable(f"subclass_edge{index}")


def build_branch(nodes: list[str], directions: list[str]) -> str:
    lines: list[str] = []
    for hop_index, direction in enumerate(directions, start=1):
        edge = edge_var(hop_index)
        pred = predicate_var(hop_index)
        current = nodes[hop_index - 1]
        nxt = nodes[hop_index]
        if direction == "forward":
            subject = current
            obj = nxt
        else:
            subject = nxt
            obj = current
        lines.append(f'BIND("{direction}" AS {direction_var(hop_index)})')
        lines.append(
            f"{edge} a rdf:Statement ; rdf:subject {subject} ; rdf:predicate {pred} ; rdf:object {obj} ."
        )
    return "\n".join(lines)


def build_endpoint_subclass_branch(
    start_iri: str,
    end_iri: str,
    path_length: int,
    directions: list[str],
    start_lifted: bool,
    end_lifted: bool,
) -> str:
    nodes = [witness_node_var(0) if start_lifted else iri_term(start_iri)]
    for index in range(1, path_length):
        nodes.append(middle_node_var(index))
    nodes.append(witness_node_var(path_length) if end_lifted else iri_term(end_iri))

    lines: list[str] = []
    if start_lifted:
        lines.append(
            f"{subclass_edge_var(0)} a rdf:Statement ; rdf:subject {witness_node_var(0)} ; "
            f"rdf:predicate <{BIOLINK_SUBCLASS_OF}> ; rdf:object <{start_iri}> ."
        )
    if end_lifted:
        lines.append(
            f"{subclass_edge_var(path_length)} a rdf:Statement ; rdf:subject {witness_node_var(path_length)} ; "
            f"rdf:predicate <{BIOLINK_SUBCLASS_OF}> ; rdf:object <{end_iri}> ."
        )
    lines.append(build_branch(nodes, directions))
    return "\n".join(lines)


def build_all_direction_branches(nodes: list[str], path_length: int) -> str:
    branches = []
    for directions in product(("forward", "reverse"), repeat=path_length):
        branches.append("{\n" + build_branch(nodes, list(directions)) + "\n}")
    return "\n  UNION\n  ".join(branches)


def build_all_endpoint_subclass_branches(start_iri: str, end_iri: str, path_length: int) -> str:
    branches = []
    for directions in product(("forward", "reverse"), repeat=path_length):
        for start_lifted, end_lifted in product((False, True), repeat=2):
            branches.append(
                "{\n"
                + build_endpoint_subclass_branch(
                    start_iri,
                    end_iri,
                    path_length,
                    list(directions),
                    start_lifted,
                    end_lifted,
                )
                + "\n}"
            )
    return "\n  UNION\n  ".join(branches)


def build_paths_query(
    start_iri: str,
    end_iri: str,
    path_length: int,
    limit: int | None = None,
    offset: int | None = None,
    include_subclasses: bool = False,
) -> str:
    if path_length < 1:
        raise ValueError("path_length must be at least 1")

    select_vars: list[str] = []
    for hop_index in range(1, path_length + 1):
        select_vars.extend([direction_var(hop_index), edge_var(hop_index), predicate_var(hop_index)])
        if hop_index < path_length:
            select_vars.extend([middle_node_var(hop_index)])
    if include_subclasses:
        select_vars.extend(
            [
                witness_node_var(0),
                subclass_edge_var(0),
                witness_node_var(path_length),
                subclass_edge_var(path_length),
            ]
        )

    path_nodes = [iri_term(start_iri)]
    for index in range(1, path_length):
        path_nodes.append(middle_node_var(index))
    path_nodes.append(iri_term(end_iri))

    query_lines = [
        f"PREFIX rdf: <{RDF_NS}>",
        "SELECT DISTINCT " + " ".join(select_vars),
        "WHERE {",
    ]
    if include_subclasses:
        query_lines.append("  " + build_all_endpoint_subclass_branches(start_iri, end_iri, path_length))
    else:
        query_lines.append("  " + build_all_direction_branches(path_nodes, path_length))
    query_lines.append("}")

    if limit is not None:
        query_lines.append(f"LIMIT {limit}")
    if offset is not None:
        query_lines.append(f"OFFSET {offset}")
    return "\n".join(query_lines)


def build_properties_query(resources: list[str]) -> str:
    values = " ".join(iri_term(resource) for resource in resources)
    return f"""PREFIX rdf: <{RDF_NS}>
SELECT ?resource ?predicate ?value
WHERE {{
  VALUES ?resource {{ {values} }}
  ?resource ?predicate ?value .
  FILTER (?predicate NOT IN (<{RDF_NS}subject>, <{RDF_NS}predicate>, <{RDF_NS}object>))
}}
ORDER BY ?resource ?predicate ?value
"""


def run_qlever_query(
    host_name: str,
    port: int,
    query: str,
    access_token: str | None = None,
) -> dict[str, Any]:
    data = urllib.parse.urlencode({"query": query}).encode("utf-8")
    request = urllib.request.Request(
        f"http://{host_name}:{port}",
        data=data,
        headers={"Accept": "text/tab-separated-values"},
        method="POST",
    )
    if access_token:
        request.add_header("Authorization", f"Bearer {access_token}")
    start = time.perf_counter()
    with urllib.request.urlopen(request) as response:
        payload = response.read().decode("utf-8")
    elapsed_ms = round((time.perf_counter() - start) * 1000)
    return {"format": "tsv", "elapsed_ms": elapsed_ms, "payload": payload}


def iter_qlever_rows(
    host_name: str,
    port: int,
    query: str,
    access_token: str | None = None,
    stats: dict[str, int] | None = None,
):
    data = urllib.parse.urlencode({"query": query}).encode("utf-8")
    request = urllib.request.Request(
        f"http://{host_name}:{port}",
        data=data,
        headers={"Accept": "text/tab-separated-values"},
        method="POST",
    )
    if access_token:
        request.add_header("Authorization", f"Bearer {access_token}")
    with closing(urllib.request.urlopen(request)) as response:
        text_stream = io.TextIOWrapper(response, encoding="utf-8", newline="")
        header_line = text_stream.readline()
        if not header_line:
            return
        header = header_line.rstrip("\r\n").split("\t")
        for line in text_stream:
            line = line.rstrip("\r\n")
            if not line:
                continue
            values = line.split("\t")
            if len(values) != len(header):
                if stats is not None:
                    stats["malformed_rows"] = stats.get("malformed_rows", 0) + 1
                continue
            yield {column: value for column, value in zip(header, values, strict=False)}


def rows_from_result(result: dict[str, Any]) -> list[dict[str, str]]:
    reader = csv.reader(io.StringIO(result["payload"]), delimiter="\t")
    try:
        header = next(reader)
    except StopIteration:
        return []
    rows: list[dict[str, str]] = []
    for values in reader:
        if not values:
            continue
        row = {column: value for column, value in zip(header, values, strict=False)}
        rows.append(row)
    return rows


def strip_typed_literal(value: str | None) -> str:
    if value is None:
        return ""
    if value.startswith('"') and '"^^<' in value:
        return value.split('"^^<', 1)[0][1:]
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def format_path_row(row: dict[str, str], path_length: int) -> dict[str, Any]:
    path: dict[str, Any] = {"steps": []}
    for index in range(1, path_length):
        path[f"node{index}"] = {
            "id": row[middle_node_var(index)],
            "label": None,
        }

    start_witness = row.get(witness_node_var(0))
    if start_witness:
        path["start_witness"] = start_witness
    start_subclass_edge = row.get(subclass_edge_var(0))
    if start_subclass_edge:
        path["start_subclass_edge"] = start_subclass_edge

    end_witness = row.get(witness_node_var(path_length))
    if end_witness:
        path["end_witness"] = end_witness
    end_subclass_edge = row.get(subclass_edge_var(path_length))
    if end_subclass_edge:
        path["end_subclass_edge"] = end_subclass_edge

    for hop_index in range(1, path_length + 1):
        step: dict[str, Any] = {
            "direction": strip_typed_literal(row[direction_var(hop_index)]),
            "edge": row[edge_var(hop_index)],
            "predicate": row[predicate_var(hop_index)],
        }
        if hop_index < path_length:
            step["next_node"] = row[middle_node_var(hop_index)]
        path["steps"].append(step)
    return path


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def fetch_properties(
    host_name: str,
    port: int,
    resources: list[str],
    access_token: str | None = None,
) -> dict[str, list[dict[str, str]]]:
    properties: dict[str, list[dict[str, str]]] = {}
    for batch in chunked(resources, 200):
        result = run_qlever_query(
            host_name,
            port,
            build_properties_query(batch),
            access_token=access_token,
        )
        for row in rows_from_result(result):
            properties.setdefault(row["?resource"], []).append(
                {"predicate": row["?predicate"], "value": row["?value"]}
            )
    return properties


def collect_resources(paths: list[dict[str, Any]], start_iri: str, end_iri: str) -> list[str]:
    resources = {start_iri, end_iri}
    for path in paths:
        for step in path["steps"]:
            resources.add(normalize_iri(step["edge"]))
            next_node = step.get("next_node")
            if next_node:
                resources.add(normalize_iri(next_node))
    return sorted(resources)


def resources_from_path(path: dict[str, Any], start_iri: str, end_iri: str) -> set[str]:
    resources = {start_iri, end_iri}
    for step in path["steps"]:
        resources.add(normalize_iri(step["edge"]))
        next_node = step.get("next_node")
        if next_node:
            resources.add(normalize_iri(next_node))
    return resources


def main() -> None:
    args = parse_args()
    start_iri = curie_to_iri(args.start_curie)
    end_iri = curie_to_iri(args.end_curie)
    query_start = time.perf_counter()
    path_count = 0
    seen_resources: set[str] = {start_iri, end_iri}
    stats: dict[str, int] = {"malformed_rows": 0}
    remaining = args.limit
    offset = 0

    sys.stdout.write("{\n")
    sys.stdout.write(f'  "end": {json.dumps(end_iri)},\n')
    sys.stdout.write('  "paths": [\n')

    first = True
    while remaining is None or remaining > 0:
        page_limit = args.page_size if remaining is None else min(args.page_size, remaining)
        query = build_paths_query(
            start_iri,
            end_iri,
            args.path_length,
            limit=page_limit,
            offset=offset,
            include_subclasses=args.include_subclasses,
        )
        page_rows = 0
        for row in iter_qlever_rows(
            args.host_name,
            args.port,
            query,
            access_token=args.access_token,
            stats=stats,
        ):
            path = format_path_row(row, args.path_length)
            if not first:
                sys.stdout.write(",\n")
            sys.stdout.write(json.dumps(path, indent=4, sort_keys=True))
            first = False
            path_count += 1
            page_rows += 1
            if args.include_properties:
                seen_resources.update(resources_from_path(path, start_iri, end_iri))
        if stats["malformed_rows"]:
            raise RuntimeError(
                f"Encountered {stats['malformed_rows']} malformed TSV rows from QLever; "
                "aborting to avoid silently truncating the result set."
            )
        if page_rows < page_limit:
            break
        offset += page_rows
        if remaining is not None:
            remaining -= page_rows

    query_elapsed_ms = round((time.perf_counter() - query_start) * 1000)
    sys.stdout.write("\n  ],\n")
    sys.stdout.write(f'  "path_count": {path_count},\n')
    sys.stdout.write(f'  "path_length": {args.path_length},\n')
    sys.stdout.write(f'  "query_time_ms": {query_elapsed_ms},\n')
    sys.stdout.write(f'  "skipped_malformed_rows": {stats["malformed_rows"]},\n')

    if args.include_properties:
        properties = fetch_properties(
            args.host_name,
            args.port,
            sorted(seen_resources),
            access_token=args.access_token,
        )
        sys.stdout.write('  "properties": ')
        json.dump(properties, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write(",\n")

    sys.stdout.write(f'  "start": {json.dumps(start_iri)}\n')
    sys.stdout.write("}\n")


if __name__ == "__main__":
    main()
