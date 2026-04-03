#!/usr/bin/env python3
import argparse
import hashlib
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import sys
from typing import Any
import urllib.error
from urllib.parse import quote, unquote

from bmt import Toolkit

from find_paths import (
    BIOLINK_VOCAB,
    IDENTIFIERS_ORG,
    RDF_NS,
    RDFS_NS,
    curie_to_iri,
    iri_term,
    rows_from_result,
    run_qlever_query,
    strip_typed_literal,
)


RDF_TYPE = RDF_NS + "type"
RDF_STATEMENT = RDF_NS + "Statement"
RDF_SUBJECT = RDF_NS + "subject"
RDF_PREDICATE = RDF_NS + "predicate"
RDF_OBJECT = RDF_NS + "object"
RDFS_LABEL = RDFS_NS + "label"
RDFS_SUBCLASS_OF = RDFS_NS + "subClassOf"
RDFS_SUBPROPERTY_OF = RDFS_NS + "subPropertyOf"
BIOLINK_SUBCLASS_OF = BIOLINK_VOCAB + "subclass_of"

KGXTR_NS = "https://w3id.org/kgx/traversal/"
KGXTR_TRAVERSAL_FROM = KGXTR_NS + "traversal_from"
KGXTR_TRAVERSAL_TO = KGXTR_NS + "traversal_to"
KGX_SLOT_NS = "https://w3id.org/kgx/slot/"
BIOLINK_ENUM_NS = "https://w3id.org/biolink/enum/"

NAME_PREDICATES = (
    RDFS_LABEL,
    BIOLINK_VOCAB + "name",
    BIOLINK_VOCAB + "full_name",
    "https://schema.org/name",
    "http://schema.org/name",
    "https://w3id.org/kgx/slot/name",
)
SOURCE_ROLE_BY_PREDICATE = {
    BIOLINK_VOCAB + "primary_knowledge_source": "primary_knowledge_source",
    BIOLINK_VOCAB + "aggregator_knowledge_source": "aggregator_knowledge_source",
    BIOLINK_VOCAB + "supporting_data_source": "supporting_data_source",
}
STRUCTURAL_EDGE_PREDICATES = {
    RDF_TYPE,
    RDF_SUBJECT,
    RDF_PREDICATE,
    RDF_OBJECT,
    KGXTR_TRAVERSAL_FROM,
    KGXTR_TRAVERSAL_TO,
}
NON_ALPHANUMERIC_RE = re.compile(r"[^A-Za-z0-9_]+")
TOOLKIT = Toolkit()
ALL_BIOLINK_ENUMS = tuple(TOOLKIT.view.all_enums().keys())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Translate a one-hop TRAPI query into a QLever query or serve it over HTTP."
    )
    parser.add_argument(
        "input",
        nargs="?",
        default="-",
        help="Path to a TRAPI request JSON file, or '-' for stdin.",
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
        "--access-token",
        default=None,
        help="Optional QLever access token.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Maximum number of matching edges to return. Default: 1000",
    )
    parser.add_argument(
        "--resource-id",
        default="infores:qlever-trapi",
        help="TRAPI `analysis.resource_id` value. Default: infores:qlever-trapi",
    )
    parser.add_argument(
        "--subclass-depth",
        type=int,
        default=1,
        help="Maximum endpoint subclass expansion depth. Use 0 to disable. Default: 1",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Run an HTTP server instead of reading a single request from stdin or a file.",
    )
    parser.add_argument(
        "--listen-host",
        default="127.0.0.1",
        help="HTTP listen host when --serve is set. Default: 127.0.0.1",
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        default=8000,
        help="HTTP listen port when --serve is set. Default: 8000",
    )
    return parser.parse_args()


def load_request(path: str) -> dict[str, Any]:
    if path == "-":
        return json.load(sys.stdin)
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def ensure_string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a string or list of strings")
    return value


def ensure_qualifier_constraints(value: Any, field_name: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")

    constraints: list[dict[str, Any]] = []
    for index, constraint in enumerate(value):
        if not isinstance(constraint, dict):
            raise ValueError(f"{field_name}[{index}] must be an object")
        qualifier_set = constraint.get("qualifier_set", [])
        if not isinstance(qualifier_set, list):
            raise ValueError(f"{field_name}[{index}].qualifier_set must be a list")
        normalized_set: list[dict[str, str]] = []
        for filter_index, qualifier_filter in enumerate(qualifier_set):
            if not qualifier_filter:
                continue
            if not isinstance(qualifier_filter, dict):
                raise ValueError(f"{field_name}[{index}].qualifier_set[{filter_index}] must be an object")
            qualifier_type_id = qualifier_filter.get("qualifier_type_id")
            qualifier_value = qualifier_filter.get("qualifier_value")
            if not isinstance(qualifier_type_id, str):
                raise ValueError(f"{field_name}[{index}].qualifier_set[{filter_index}].qualifier_type_id must be a string")
            if not isinstance(qualifier_value, str):
                raise ValueError(f"{field_name}[{index}].qualifier_set[{filter_index}].qualifier_value must be a string")
            normalized_set.append(
                {
                    "qualifier_type_id": qualifier_type_id,
                    "qualifier_value": qualifier_value,
                }
            )
        constraints.append({"qualifier_set": normalized_set})
    return constraints


def validate_qnode(qnode_id: str, qnode: dict[str, Any]) -> dict[str, Any]:
    if qnode.get("is_set") is True:
        raise ValueError(f"qnode {qnode_id} uses is_set=true, which is not supported yet")
    unsupported = {"constraints", "set_interpretation"} & set(qnode)
    if unsupported:
        field_list = ", ".join(sorted(unsupported))
        raise ValueError(f"qnode {qnode_id} uses unsupported fields: {field_list}")
    return {
        "qnode_id": qnode_id,
        "ids": ensure_string_list(qnode.get("ids"), f"qnode {qnode_id}.ids"),
        "categories": ensure_string_list(qnode.get("categories"), f"qnode {qnode_id}.categories"),
    }


def validate_qedge(qedge_id: str, qedge: dict[str, Any], qnodes: dict[str, Any]) -> dict[str, Any]:
    subject = qedge.get("subject")
    object_ = qedge.get("object")
    if not isinstance(subject, str) or subject not in qnodes:
        raise ValueError(f"qedge {qedge_id} subject must reference an existing qnode")
    if not isinstance(object_, str) or object_ not in qnodes:
        raise ValueError(f"qedge {qedge_id} object must reference an existing qnode")

    unsupported = {"attribute_constraints"} & set(qedge)
    if unsupported:
        field_list = ", ".join(sorted(unsupported))
        raise ValueError(f"qedge {qedge_id} uses unsupported fields: {field_list}")

    knowledge_type = qedge.get("knowledge_type")
    if knowledge_type not in (None, "lookup"):
        raise ValueError(f"qedge {qedge_id} knowledge_type={knowledge_type!r} is not supported")

    return {
        "qedge_id": qedge_id,
        "subject": subject,
        "object": object_,
        "predicates": ensure_string_list(qedge.get("predicates"), f"qedge {qedge_id}.predicates"),
        "qualifier_constraints": ensure_qualifier_constraints(
            qedge.get("qualifier_constraints"),
            f"qedge {qedge_id}.qualifier_constraints",
        ),
    }


def normalize_trapi_request(request: dict[str, Any], subclass_depth: int = 1) -> dict[str, Any]:
    message = request.get("message")
    if not isinstance(message, dict):
        raise ValueError("TRAPI request must contain a top-level message object")

    query_graph = message.get("query_graph")
    if not isinstance(query_graph, dict):
        raise ValueError("TRAPI request message must contain query_graph")

    qnodes = query_graph.get("nodes")
    qedges = query_graph.get("edges")
    if not isinstance(qnodes, dict) or not qnodes:
        raise ValueError("query_graph.nodes must be a non-empty object")
    if not isinstance(qedges, dict) or not qedges:
        raise ValueError("query_graph.edges must be a non-empty object")

    normalized_qnodes: dict[str, Any] = {}
    for qnode_id, raw_qnode in qnodes.items():
        if not isinstance(raw_qnode, dict):
            raise ValueError(f"query_graph node {qnode_id} must be an object")
        normalized_qnodes[qnode_id] = validate_qnode(qnode_id, raw_qnode)

    normalized_qedges: dict[str, Any] = {}
    referenced_qnodes: set[str] = set()
    for qedge_id, raw_qedge in qedges.items():
        if not isinstance(raw_qedge, dict):
            raise ValueError(f"query_graph edge {qedge_id} must be an object")
        normalized_qedge = validate_qedge(qedge_id, raw_qedge, normalized_qnodes)
        normalized_qedges[qedge_id] = normalized_qedge
        referenced_qnodes.add(normalized_qedge["subject"])
        referenced_qnodes.add(normalized_qedge["object"])

    unreferenced_qnodes = [qnode_id for qnode_id in normalized_qnodes if qnode_id not in referenced_qnodes]
    if unreferenced_qnodes:
        raise ValueError(
            "Every qnode must participate in at least one qedge: "
            + ", ".join(unreferenced_qnodes)
        )

    internal_qnodes, internal_qedges = rewrite_query_graph_for_subclass(
        normalized_qnodes,
        normalized_qedges,
        subclass_depth=subclass_depth,
    )
    assign_indexes(internal_qnodes)
    assign_indexes(internal_qedges)

    return {
        "message": message,
        "query_graph": query_graph,
        "original_qnodes": normalized_qnodes,
        "original_qedges": normalized_qedges,
        "qnodes": internal_qnodes,
        "qedges": internal_qedges,
    }


def values_clause(variable: str, iris: list[str]) -> str:
    return f"VALUES {variable} {{ {' '.join(iri_term(iri) for iri in iris)} }}"


def safe_name(value: str) -> str:
    return value.replace(" ", "_")


def custom_slot_iri(key: str) -> str:
    return KGX_SLOT_NS + quote(safe_name(key), safe="._-")


def enum_value_iri(enum_name: str, value: str) -> str:
    return BIOLINK_ENUM_NS + quote(enum_name, safe="._-/") + "/" + quote(value, safe="._-")


def assign_indexes(records: dict[str, dict[str, Any]]) -> None:
    for index, record in enumerate(records.values()):
        record["index"] = index


def rewrite_query_graph_for_subclass(
    qnodes: dict[str, dict[str, Any]],
    qedges: dict[str, dict[str, Any]],
    subclass_depth: int,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    if subclass_depth < 0:
        raise ValueError("subclass_depth must be non-negative")

    internal_qnodes = {qnode_id: dict(qnode) for qnode_id, qnode in qnodes.items()}
    internal_qedges = {qedge_id: dict(qedge) for qedge_id, qedge in qedges.items()}
    if subclass_depth == 0 or not internal_qedges:
        return internal_qnodes, internal_qedges

    qnode_ids_with_hierarchy_edges: set[str] = set()
    for qedge in internal_qedges.values():
        predicates = set(qedge.get("predicates", []))
        if {"biolink:subclass_of", "biolink:superclass_of"} & predicates:
            qnode_ids_with_hierarchy_edges.add(qedge["subject"])
            qnode_ids_with_hierarchy_edges.add(qedge["object"])

    synthetic_qnodes: dict[str, dict[str, Any]] = {}
    synthetic_qedges: dict[str, dict[str, Any]] = {}
    for qnode_id, qnode in internal_qnodes.items():
        if not qnode.get("ids") or qnode_id in qnode_ids_with_hierarchy_edges:
            continue
        superclass_qnode_id = f"{qnode_id}_superclass"
        synthetic_qnodes[superclass_qnode_id] = {
            "qnode_id": superclass_qnode_id,
            "ids": list(qnode["ids"]),
            "categories": list(qnode["categories"]),
            "_superclass": True,
            "_original_qnode_id": qnode_id,
        }
        qnode["ids"] = []
        qnode["categories"] = []
        subclass_qedge_id = f"{qnode_id}_subclass_edge"
        synthetic_qedges[subclass_qedge_id] = {
            "qedge_id": subclass_qedge_id,
            "subject": qnode_id,
            "object": superclass_qnode_id,
            "predicates": ["biolink:subclass_of"],
            "_subclass": True,
            "_max_path_length": subclass_depth,
            "_original_qnode_id": qnode_id,
        }

    internal_qnodes.update(synthetic_qnodes)
    internal_qedges.update(synthetic_qedges)
    return internal_qnodes, internal_qedges


def safe_var_suffix(value: str) -> str:
    suffix = NON_ALPHANUMERIC_RE.sub("_", value)
    if not suffix:
        return "x"
    if suffix[0].isdigit():
        return "x_" + suffix
    return suffix


def qnode_binding_var(qnode: dict[str, Any]) -> str:
    return f"?node_{qnode['index']}_{safe_var_suffix(qnode['qnode_id'])}"


def qnode_category_var(qnode: dict[str, Any]) -> str:
    return f"?node_category_{qnode['index']}_{safe_var_suffix(qnode['qnode_id'])}"


def qedge_binding_var(qedge: dict[str, Any]) -> str:
    return f"?edge_{qedge['index']}_{safe_var_suffix(qedge['qedge_id'])}"


def qedge_predicate_var(qedge: dict[str, Any]) -> str:
    return f"?predicate_{qedge['index']}_{safe_var_suffix(qedge['qedge_id'])}"


def qedge_predicate_filter_var(qedge: dict[str, Any]) -> str:
    return f"?predicate_filter_{qedge['index']}_{safe_var_suffix(qedge['qedge_id'])}"


def qedge_qualifier_predicate_var(qedge: dict[str, Any], constraint_index: int, filter_index: int) -> str:
    return f"?qualifier_predicate_{qedge['index']}_{constraint_index}_{filter_index}"


def qedge_qualifier_value_var(qedge: dict[str, Any], constraint_index: int, filter_index: int) -> str:
    return f"?qualifier_value_{qedge['index']}_{constraint_index}_{filter_index}"


def qualifier_type_name(qualifier_type_id: str) -> str:
    return qualifier_type_id.removeprefix("biolink:")


def qualifier_predicate_iris(qualifier_type_id: str) -> list[str]:
    qualifier_name = qualifier_type_name(qualifier_type_id)
    candidate_iris = [
        curie_to_iri(f"biolink:{qualifier_name}"),
        custom_slot_iri(qualifier_name),
    ]
    return dedupe(candidate_iris)


def qualifier_value_iris(qualifier_type_id: str, qualifier_value: str) -> list[str]:
    qualifier_name = qualifier_type_name(qualifier_type_id)
    if not TOOLKIT.is_qualifier(qualifier_name):
        raise ValueError(f"Invalid qualifier in query: {qualifier_name}")

    if qualifier_name == "qualified_predicate":
        return [curie_to_iri(qualifier_value)]

    qualifier_value_plus_descendants = [qualifier_value]
    permissible_value = False
    for enum_name in ALL_BIOLINK_ENUMS:
        if TOOLKIT.is_permissible_value_of_enum(enum_name=enum_name, value=qualifier_value):
            permissible_value = True
            qualifier_value_plus_descendants.extend(
                TOOLKIT.get_permissible_value_descendants(
                    permissible_value=qualifier_value,
                    enum_name=enum_name,
                )
            )
    if not permissible_value:
        raise ValueError(f"Invalid value for qualifier {qualifier_name} in query: {qualifier_value}")

    return dedupe(
        [enum_value_iri(enum_name, value)
         for enum_name in ALL_BIOLINK_ENUMS
         if TOOLKIT.is_permissible_value_of_enum(enum_name=enum_name, value=qualifier_value)
         for value in set(qualifier_value_plus_descendants)]
    )


def qualifier_type_id_from_predicate(predicate: str) -> str | None:
    candidates: list[str] = []
    if predicate.startswith(KGX_SLOT_NS):
        candidates.append(unquote(predicate[len(KGX_SLOT_NS) :]))
    if predicate.startswith(BIOLINK_VOCAB):
        candidates.append(unquote(predicate[len(BIOLINK_VOCAB) :]))
    for candidate in candidates:
        if TOOLKIT.is_qualifier(candidate):
            return f"biolink:{candidate}"
    return None


def decode_qualifier_value(value: str) -> Any:
    if value.startswith(BIOLINK_ENUM_NS):
        return unquote(value.rsplit("/", 1)[1])
    return decode_typed_literal(value)


def append_node_filters(lines: list[str], qnode: dict[str, Any]) -> None:
    variable = qnode_binding_var(qnode)
    ids = [curie_to_iri(value) for value in qnode["ids"]]
    if ids:
        lines.append(f"  {values_clause(variable, ids)}")

    categories = [curie_to_iri(value) for value in qnode["categories"]]
    if categories:
        category_var = qnode_category_var(qnode)
        lines.append(f"  {values_clause(category_var, categories)}")
        lines.append(f"  {variable} <{RDF_TYPE}>/<{RDFS_SUBCLASS_OF}>* {category_var} .")


def append_edge_filters(lines: list[str], qedge: dict[str, Any]) -> None:
    predicates = qedge["predicates"]
    predicate_iris = [curie_to_iri(value) for value in predicates]
    if predicate_iris:
        predicate_var = qedge_predicate_filter_var(qedge)
        lines.append(f"  {values_clause(predicate_var, predicate_iris)}")
        lines.append(f"  {qedge_predicate_var(qedge)} <{RDFS_SUBPROPERTY_OF}>* {predicate_var} .")

    qualifier_constraint_lines = build_qualifier_constraint_lines(qedge)
    lines.extend(qualifier_constraint_lines)


def build_qualifier_constraint_lines(qedge: dict[str, Any]) -> list[str]:
    constraints = qedge.get("qualifier_constraints", [])
    branches: list[str] = []
    for constraint_index, constraint in enumerate(constraints):
        qualifier_set = constraint.get("qualifier_set", [])
        if not qualifier_set:
            continue
        branch_lines = ["  {"]
        for filter_index, qualifier_filter in enumerate(qualifier_set):
            predicate_var = qedge_qualifier_predicate_var(qedge, constraint_index, filter_index)
            value_var = qedge_qualifier_value_var(qedge, constraint_index, filter_index)
            branch_lines.append(
                f"    {values_clause(predicate_var, qualifier_predicate_iris(qualifier_filter['qualifier_type_id']))}"
            )
            branch_lines.append(
                f"    {values_clause(value_var, qualifier_value_iris(qualifier_filter['qualifier_type_id'], qualifier_filter['qualifier_value']))}"
            )
            branch_lines.append(f"    {qedge_binding_var(qedge)} {predicate_var} {value_var} .")
        branch_lines.append("  }")
        branches.append("\n".join(branch_lines))

    if not branches:
        return []

    lines: list[str] = []
    for index, branch in enumerate(branches):
        if index:
            lines.append("  UNION")
        lines.extend(branch.splitlines())
    return lines


def build_trapi_query(normalized_request: dict[str, Any], limit: int | None = None) -> str:
    qnode_vars = [qnode_binding_var(qnode) for qnode in normalized_request["qnodes"].values()]
    qedge_vars = [qedge_binding_var(qedge) for qedge in normalized_request["qedges"].values()]
    predicate_vars = [
        qedge_predicate_var(qedge)
        for qedge in normalized_request["qedges"].values()
        if not qedge.get("_subclass", False)
    ]

    lines = [
        f"PREFIX rdf: <{RDF_NS}>",
        "",
        f"SELECT DISTINCT {' '.join(qnode_vars + qedge_vars + predicate_vars)}",
        "WHERE {",
    ]

    for qedge in normalized_request["qedges"].values():
        if qedge.get("_subclass", False):
            lines.extend(build_subclass_union_lines(normalized_request, qedge))
        else:
            lines.extend(
                [
                    f"  {qedge_binding_var(qedge)} a rdf:Statement ;",
                    f"    rdf:subject {qnode_binding_var(normalized_request['qnodes'][qedge['subject']])} ;",
                    f"    rdf:predicate {qedge_predicate_var(qedge)} ;",
                    f"    rdf:object {qnode_binding_var(normalized_request['qnodes'][qedge['object']])} .",
                ]
            )
            append_edge_filters(lines, qedge)

    for qnode in normalized_request["qnodes"].values():
        append_node_filters(lines, qnode)

    lines.extend(
        [
            "}",
            f"ORDER BY {' '.join(qnode_vars + qedge_vars)}",
        ]
    )
    if limit is not None:
        lines.append(f"LIMIT {limit}")
    return "\n".join(lines) + "\n"


def build_subclass_union_lines(normalized_request: dict[str, Any], qedge: dict[str, Any]) -> list[str]:
    actual_var = qnode_binding_var(normalized_request["qnodes"][qedge["subject"]])
    superclass_var = qnode_binding_var(normalized_request["qnodes"][qedge["object"]])
    path_var = qedge_binding_var(qedge)
    max_path_length = qedge["_max_path_length"]

    branches: list[str] = []
    for depth in range(max_path_length + 1):
        branch_lines = ["  {"]
        if depth == 0:
            branch_lines.append(f"    FILTER({actual_var} = {superclass_var})")
            branch_lines.append(f'    BIND("" AS {path_var})')
        else:
            edge_vars: list[str] = []
            previous_node = actual_var
            for hop_index in range(depth):
                edge_var = f"{path_var}_hop_{hop_index}"
                edge_vars.append(edge_var)
                next_node = superclass_var if hop_index == depth - 1 else f"{path_var}_node_{hop_index}"
                branch_lines.extend(
                    [
                        f"    {edge_var} a rdf:Statement ;",
                        f"      rdf:subject {previous_node} ;",
                        f"      rdf:predicate <{BIOLINK_SUBCLASS_OF}> ;",
                        f"      rdf:object {next_node} .",
                    ]
                )
                previous_node = next_node
            if len(edge_vars) > 1:
                for left_index in range(len(edge_vars)):
                    for right_index in range(left_index + 1, len(edge_vars)):
                        branch_lines.append(f"    FILTER({edge_vars[left_index]} != {edge_vars[right_index]})")
            branch_lines.append(f"    BIND({concat_expression(edge_vars)} AS {path_var})")
        branch_lines.append("  }")
        branches.append("\n".join(branch_lines))

    union_lines: list[str] = []
    for index, branch in enumerate(branches):
        if index:
            union_lines.append("  UNION")
        union_lines.extend(branch.splitlines())
    return union_lines


def concat_expression(edge_vars: list[str]) -> str:
    if len(edge_vars) == 1:
        return f"STR({edge_vars[0]})"
    expression = f'CONCAT(STR({edge_vars[0]}), "||", STR({edge_vars[1]}))'
    for edge_var in edge_vars[2:]:
        expression = f'CONCAT({expression}, "||", STR({edge_var}))'
    return expression


def iri_to_curie(value: str) -> str:
    if value.startswith(IDENTIFIERS_ORG):
        return unquote(value[len(IDENTIFIERS_ORG) :])
    if value.startswith(BIOLINK_VOCAB):
        return "biolink:" + value[len(BIOLINK_VOCAB) :]
    return value


def unescape_literal(value: str) -> str:
    return (
        value.replace("\\t", "\t")
        .replace("\\r", "\r")
        .replace("\\n", "\n")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
    )


def decode_typed_literal(value: str) -> Any:
    if not value.startswith('"'):
        return iri_to_curie(value)
    if '"^^<' not in value:
        return unescape_literal(strip_typed_literal(value))

    literal, datatype = value.rsplit('"^^<', 1)
    text = unescape_literal(literal[1:])
    datatype = datatype[:-1]
    if datatype.endswith("#boolean"):
        return text == "true"
    if datatype.endswith("#integer"):
        return int(text)
    if datatype.endswith("#double"):
        return float(text)
    return text


def dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def select_node_name(properties: list[dict[str, str]]) -> str | None:
    by_predicate: dict[str, list[str]] = {}
    for prop in properties:
        by_predicate.setdefault(prop["predicate"], []).append(prop["value"])
    for predicate in NAME_PREDICATES:
        if predicate in by_predicate:
            return str(decode_typed_literal(by_predicate[predicate][0]))
    return None


def extract_categories(properties: list[dict[str, str]]) -> list[str]:
    categories: list[str] = []
    for prop in properties:
        if prop["predicate"] != RDF_TYPE:
            continue
        category = iri_to_curie(prop["value"])
        if category == RDF_STATEMENT:
            continue
        categories.append(category)
    return dedupe(categories)


def build_trapi_attributes(
    properties: list[dict[str, str]],
    exclude_predicates: set[str],
) -> list[dict[str, Any]]:
    attributes: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for prop in properties:
        predicate = prop["predicate"]
        if predicate in exclude_predicates:
            continue
        value = decode_typed_literal(prop["value"])
        key = (predicate, json.dumps(value, sort_keys=True, default=str))
        if key in seen:
            continue
        seen.add(key)
        attributes.append(
            {
                "attribute_type_id": iri_to_curie(predicate),
                "value": value,
            }
        )
    return attributes


def build_sources(properties: list[dict[str, str]]) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for prop in properties:
        role = SOURCE_ROLE_BY_PREDICATE.get(prop["predicate"])
        if role is None:
            continue
        resource_id = str(decode_typed_literal(prop["value"]))
        key = (resource_id, role)
        if key in seen:
            continue
        seen.add(key)
        sources.append({"resource_id": resource_id, "resource_role": role})
    return sources


def build_qualifiers(properties: list[dict[str, str]]) -> list[dict[str, Any]]:
    qualifiers: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for prop in properties:
        qualifier_type_id = qualifier_type_id_from_predicate(prop["predicate"])
        if qualifier_type_id is None:
            continue
        qualifier_value = decode_qualifier_value(prop["value"])
        key = (qualifier_type_id, json.dumps(qualifier_value, sort_keys=True, default=str))
        if key in seen:
            continue
        seen.add(key)
        qualifiers.append(
            {
                "qualifier_type_id": qualifier_type_id,
                "qualifier_value": qualifier_value,
            }
        )
    return qualifiers


def qualifier_predicates_in_properties(properties: list[dict[str, str]]) -> set[str]:
    return {prop["predicate"] for prop in properties if qualifier_type_id_from_predicate(prop["predicate"]) is not None}


def resource_values_by_predicate(properties: list[dict[str, str]]) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for prop in properties:
        values.setdefault(prop["predicate"], []).append(prop["value"])
    return values


def edge_payload_from_properties(edge_iri: str, properties: dict[str, list[dict[str, str]]]) -> dict[str, Any]:
    edge_props = properties.get(edge_iri, [])
    by_predicate = resource_values_by_predicate(edge_props)
    try:
        subject_iri = by_predicate[RDF_SUBJECT][0]
        predicate_iri = by_predicate[RDF_PREDICATE][0]
        object_iri = by_predicate[RDF_OBJECT][0]
    except KeyError as exc:
        raise ValueError(f"Missing structural edge metadata for {edge_iri}: {exc.args[0]}") from exc

    edge_payload: dict[str, Any] = {
        "subject": iri_to_curie(subject_iri),
        "predicate": iri_to_curie(predicate_iri),
        "object": iri_to_curie(object_iri),
    }
    sources = build_sources(edge_props)
    if sources:
        edge_payload["sources"] = sources
    qualifiers = build_qualifiers(edge_props)
    if qualifiers:
        edge_payload["qualifiers"] = qualifiers
    edge_attributes = build_trapi_attributes(
        edge_props,
        exclude_predicates=STRUCTURAL_EDGE_PREDICATES | set(SOURCE_ROLE_BY_PREDICATE) | qualifier_predicates_in_properties(edge_props),
    )
    if edge_attributes:
        edge_payload["attributes"] = edge_attributes
    return edge_payload


def build_knowledge_graph_nodes(
    node_iris: list[str],
    properties: dict[str, list[dict[str, str]]],
) -> dict[str, Any]:
    nodes: dict[str, Any] = {}
    for node_iri in node_iris:
        node_key = iri_to_curie(node_iri)
        if node_key in nodes:
            continue
        node_props = properties.get(node_iri, [])
        node_payload: dict[str, Any] = {
            "categories": extract_categories(node_props),
        }
        node_name = select_node_name(node_props)
        if node_name:
            node_payload["name"] = node_name
        node_attributes = build_trapi_attributes(
            node_props,
            exclude_predicates={RDF_TYPE, *NAME_PREDICATES},
        )
        if node_attributes:
            node_payload["attributes"] = node_attributes
        nodes[node_key] = node_payload
    return nodes


def build_knowledge_graph_edges(
    edge_iris: list[str],
    properties: dict[str, list[dict[str, str]]],
) -> dict[str, Any]:
    edges: dict[str, Any] = {}
    for edge_iri in edge_iris:
        edge_key = iri_to_curie(edge_iri)
        if edge_key in edges:
            continue
        edges[edge_key] = edge_payload_from_properties(edge_iri, properties)
    return edges


def support_edge_ids_from_row(qedge: dict[str, Any], row: dict[str, str]) -> list[str]:
    value = row.get(qedge_binding_var(qedge), "")
    if not value:
        return []
    return [edge_id for edge_id in value.split("||") if edge_id]


def inferred_edge_id(
    main_edge_id: str,
    support_edge_ids: list[str],
    superclass_node_ids: dict[str, str],
) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {
                "main_edge_id": main_edge_id,
                "support_edge_ids": support_edge_ids,
                "superclass_node_ids": superclass_node_ids,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return f"inferred:{digest}"


def auxiliary_graph_id(edge_id: str) -> str:
    return "aux_" + edge_id.split(":", 1)[1]


def build_result_key(node_bindings: dict[str, list[dict[str, Any]]]) -> str:
    return json.dumps(
        {qnode_id: binding[0]["id"] if binding else None for qnode_id, binding in node_bindings.items()},
        sort_keys=True,
    )


def build_resource_properties_query(resources: list[str], include_structural: bool = False) -> str:
    values = " ".join(iri_term(resource) for resource in resources)
    filter_clause = (
        ""
        if include_structural
        else f"  FILTER (?predicate NOT IN (<{RDF_NS}subject>, <{RDF_NS}predicate>, <{RDF_NS}object>))\n"
    )
    return f"""SELECT ?resource ?predicate ?value
WHERE {{
  VALUES ?resource {{ {values} }}
  ?resource ?predicate ?value .
{filter_clause}}}
ORDER BY ?resource ?predicate ?value
"""


def build_knowledge_graph(
    node_iris: list[str],
    edge_iris: list[str],
    properties: dict[str, list[dict[str, str]]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        build_knowledge_graph_nodes(node_iris, properties),
        build_knowledge_graph_edges(edge_iris, properties),
    )


def qnodes_with_superclass_nodes(normalized_request: dict[str, Any]) -> set[str]:
    return {
        qnode_id
        for qnode_id in normalized_request["original_qnodes"]
        if f"{qnode_id}_superclass" in normalized_request["qnodes"]
    }


def qedges_with_attached_subclass_edges(normalized_request: dict[str, Any]) -> dict[str, list[tuple[str, str, str]]]:
    attached: dict[str, list[tuple[str, str, str]]] = {}
    superclass_qnodes = qnodes_with_superclass_nodes(normalized_request)
    for qedge_id, qedge in normalized_request["original_qedges"].items():
        bindings: list[tuple[str, str, str]] = []
        if qedge["subject"] in superclass_qnodes:
            bindings.append(("subject", f"{qedge['subject']}_subclass_edge", f"{qedge['subject']}_superclass"))
        if qedge["object"] in superclass_qnodes:
            bindings.append(("object", f"{qedge['object']}_subclass_edge", f"{qedge['object']}_superclass"))
        if bindings:
            attached[qedge_id] = bindings
    return attached


def build_results(
    normalized_request: dict[str, Any],
    rows: list[dict[str, str]],
    resource_id: str,
    knowledge_graph_edges: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    aux_graphs: dict[str, Any] = {}
    attached_subclass_edges = qedges_with_attached_subclass_edges(normalized_request)

    for row in rows:
        node_bindings: dict[str, list[dict[str, Any]]] = {}
        for qnode_id, qnode in normalized_request["original_qnodes"].items():
            actual_node_id = iri_to_curie(row[qnode_binding_var(normalized_request["qnodes"][qnode_id])])
            superclass_qnode_id = f"{qnode_id}_superclass"
            if superclass_qnode_id in normalized_request["qnodes"]:
                superclass_node_id = iri_to_curie(row[qnode_binding_var(normalized_request["qnodes"][superclass_qnode_id])])
                binding_node_id = actual_node_id if actual_node_id == superclass_node_id else superclass_node_id
            else:
                binding_node_id = actual_node_id
            node_bindings[qnode_id] = [{"id": binding_node_id}]

        edge_bindings: dict[str, list[dict[str, Any]]] = {}
        for qedge_id, qedge in normalized_request["original_qedges"].items():
            real_edge_iri = row[qedge_binding_var(normalized_request["qedges"][qedge_id])]
            real_edge_id = iri_to_curie(real_edge_iri)

            support_edge_ids: list[str] = []
            superclass_node_ids: dict[str, str] = {}
            for endpoint, subclass_qedge_id, superclass_qnode_id in attached_subclass_edges.get(qedge_id, []):
                subclass_qedge = normalized_request["qedges"][subclass_qedge_id]
                subclass_support_iris = support_edge_ids_from_row(subclass_qedge, row)
                if subclass_support_iris:
                    support_edge_ids.extend(iri_to_curie(edge_iri) for edge_iri in subclass_support_iris)
                    superclass_node_ids[endpoint] = iri_to_curie(
                        row[qnode_binding_var(normalized_request["qnodes"][superclass_qnode_id])]
                    )

            if support_edge_ids:
                inferred_id = inferred_edge_id(real_edge_id, support_edge_ids, superclass_node_ids)
                aux_id = auxiliary_graph_id(inferred_id)
                if aux_id not in aux_graphs:
                    aux_graphs[aux_id] = {
                        "edges": [real_edge_id] + support_edge_ids,
                        "attributes": [],
                    }
                if inferred_id not in knowledge_graph_edges:
                    real_edge = knowledge_graph_edges[real_edge_id]
                    inferred_edge: dict[str, Any] = {
                        "subject": superclass_node_ids.get("subject", real_edge["subject"]),
                        "predicate": real_edge["predicate"],
                        "object": superclass_node_ids.get("object", real_edge["object"]),
                        "attributes": [
                            {
                                "attribute_type_id": "biolink:knowledge_level",
                                "value": "logical_entailment",
                            },
                            {
                                "attribute_type_id": "biolink:agent_type",
                                "value": "automated_agent",
                            },
                            {
                                "attribute_type_id": "biolink:support_graphs",
                                "value": [aux_id],
                            },
                        ],
                        "sources": [
                            {
                                "resource_id": resource_id,
                                "resource_role": "primary_knowledge_source",
                            }
                        ],
                    }
                    knowledge_graph_edges[inferred_id] = inferred_edge
                edge_bindings[qedge_id] = [{"id": inferred_id}]
            else:
                edge_bindings[qedge_id] = [{"id": real_edge_id}]

        result_key = build_result_key(node_bindings)
        if result_key not in results:
            results[result_key] = {
                "node_bindings": node_bindings,
                "analyses": [
                    {
                        "resource_id": resource_id,
                        "edge_bindings": edge_bindings,
                    }
                ],
            }
            continue

        existing_edge_bindings = results[result_key]["analyses"][0]["edge_bindings"]
        for qedge_id, binding_list in edge_bindings.items():
            existing_edge_ids = {binding["id"] for binding in existing_edge_bindings.setdefault(qedge_id, [])}
            for binding in binding_list:
                if binding["id"] not in existing_edge_ids:
                    existing_edge_bindings[qedge_id].append(binding)

    return list(results.values()), knowledge_graph_edges, aux_graphs


def fetch_properties_for_resources(
    host_name: str,
    port: int,
    resources: list[str],
    access_token: str | None = None,
    include_structural: bool = False,
) -> dict[str, list[dict[str, str]]]:
    if not resources:
        return {}
    result = run_qlever_query(
        host_name,
        port,
        build_resource_properties_query(resources, include_structural=include_structural),
        access_token=access_token,
    )
    properties: dict[str, list[dict[str, str]]] = {}
    for row in rows_from_result(result):
        properties.setdefault(row["?resource"], []).append(
            {
                "predicate": row["?predicate"],
                "value": row["?value"],
            }
        )
    return properties


def answer_trapi_request(
    request: dict[str, Any],
    host_name: str = "localhost",
    port: int = 8888,
    access_token: str | None = None,
    limit: int = 1000,
    resource_id: str = "infores:qlever-trapi",
    subclass_depth: int = 1,
) -> dict[str, Any]:
    normalized = normalize_trapi_request(request, subclass_depth=subclass_depth)
    query = build_trapi_query(normalized, limit=limit)
    result = run_qlever_query(
        host_name,
        port,
        query,
        access_token=access_token,
    )
    rows = rows_from_result(result)

    node_resources: list[str] = []
    edge_resources: list[str] = []
    for row in rows:
        for qnode in normalized["qnodes"].values():
            node_resources.append(row[qnode_binding_var(qnode)])
        for qedge in normalized["qedges"].values():
            if qedge.get("_subclass", False):
                edge_resources.extend(support_edge_ids_from_row(qedge, row))
            else:
                edge_resources.append(row[qedge_binding_var(qedge)])
    node_resources = dedupe(node_resources)
    edge_resources = dedupe(edge_resources)
    resources = dedupe(node_resources + edge_resources)
    properties = fetch_properties_for_resources(
        host_name,
        port,
        resources,
        access_token=access_token,
        include_structural=True,
    )
    nodes, edges = build_knowledge_graph(node_resources, edge_resources, properties)
    results, edges, auxiliary_graphs = build_results(normalized, rows, resource_id, edges)

    return {
        "message": {
            "query_graph": normalized["query_graph"],
            "knowledge_graph": {
                "nodes": nodes,
                "edges": edges,
            },
            "results": results,
            "auxiliary_graphs": auxiliary_graphs,
        }
    }


def response_envelope(
    status: str,
    description: str,
    http_code: int,
    message: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "status": status,
        "description": description,
        "http_code": http_code,
    }
    if message is not None:
        body["message"] = message
    return body


def json_response_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_json_response(
    handler: BaseHTTPRequestHandler,
    payload: dict[str, Any],
    http_status: HTTPStatus,
) -> None:
    encoded = json_response_bytes(payload)
    handler.send_response(http_status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


def read_json_request(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    content_length = int(handler.headers.get("Content-Length", "0"))
    raw_body = handler.rfile.read(content_length)
    body = json.loads(raw_body.decode("utf-8"))
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object")
    return body


def make_trapi_handler(
    qlever_host_name: str,
    qlever_port: int,
    access_token: str | None,
    limit: int,
    resource_id: str,
    subclass_depth: int,
):
    class TrapiHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                write_json_response(
                    self,
                    response_envelope(
                        status="Success",
                        description="TRAPI service is healthy",
                        http_code=HTTPStatus.OK,
                    ),
                    HTTPStatus.OK,
                )
                return
            write_json_response(
                self,
                response_envelope(
                    status="NotFound",
                    description=f"Unknown endpoint: {self.path}",
                    http_code=HTTPStatus.NOT_FOUND,
                ),
                HTTPStatus.NOT_FOUND,
            )

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/query":
                write_json_response(
                    self,
                    response_envelope(
                        status="NotFound",
                        description=f"Unknown endpoint: {self.path}",
                        http_code=HTTPStatus.NOT_FOUND,
                    ),
                    HTTPStatus.NOT_FOUND,
                )
                return

            try:
                request = read_json_request(self)
                response = answer_trapi_request(
                    request,
                    host_name=qlever_host_name,
                    port=qlever_port,
                    access_token=access_token,
                    limit=limit,
                    resource_id=resource_id,
                    subclass_depth=subclass_depth,
                )
            except json.JSONDecodeError as exc:
                write_json_response(
                    self,
                    response_envelope(
                        status="BadRequest",
                        description=f"Invalid JSON: {exc}",
                        http_code=HTTPStatus.BAD_REQUEST,
                    ),
                    HTTPStatus.BAD_REQUEST,
                )
                return
            except ValueError as exc:
                write_json_response(
                    self,
                    response_envelope(
                        status="BadRequest",
                        description=str(exc),
                        http_code=HTTPStatus.BAD_REQUEST,
                    ),
                    HTTPStatus.BAD_REQUEST,
                )
                return
            except urllib.error.URLError as exc:
                write_json_response(
                    self,
                    response_envelope(
                        status="UpstreamError",
                        description=f"QLever request failed: {exc}",
                        http_code=HTTPStatus.BAD_GATEWAY,
                    ),
                    HTTPStatus.BAD_GATEWAY,
                )
                return
            except Exception as exc:
                write_json_response(
                    self,
                    response_envelope(
                        status="InternalError",
                        description=f"Unhandled server error: {exc}",
                        http_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                    ),
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return

            write_json_response(
                self,
                response_envelope(
                    status="Success",
                    description="Query processed successfully",
                    http_code=HTTPStatus.OK,
                    message=response["message"],
                ),
                HTTPStatus.OK,
            )

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

    return TrapiHandler


def create_trapi_http_server(
    listen_host: str,
    listen_port: int,
    qlever_host_name: str,
    qlever_port: int,
    access_token: str | None,
    limit: int,
    resource_id: str,
    subclass_depth: int,
) -> ThreadingHTTPServer:
    handler = make_trapi_handler(
        qlever_host_name=qlever_host_name,
        qlever_port=qlever_port,
        access_token=access_token,
        limit=limit,
        resource_id=resource_id,
        subclass_depth=subclass_depth,
    )
    return ThreadingHTTPServer((listen_host, listen_port), handler)


def serve_http(
    listen_host: str,
    listen_port: int,
    qlever_host_name: str,
    qlever_port: int,
    access_token: str | None,
    limit: int,
    resource_id: str,
    subclass_depth: int,
) -> None:
    server = create_trapi_http_server(
        listen_host=listen_host,
        listen_port=listen_port,
        qlever_host_name=qlever_host_name,
        qlever_port=qlever_port,
        access_token=access_token,
        limit=limit,
        resource_id=resource_id,
        subclass_depth=subclass_depth,
    )
    print(f"Listening on http://{listen_host}:{listen_port}", file=sys.stderr)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main() -> None:
    args = parse_args()
    if args.serve:
        serve_http(
            listen_host=args.listen_host,
            listen_port=args.listen_port,
            qlever_host_name=args.host_name,
            qlever_port=args.port,
            access_token=args.access_token,
            limit=args.limit,
            resource_id=args.resource_id,
            subclass_depth=args.subclass_depth,
        )
        return
    request = load_request(args.input)
    response = answer_trapi_request(
        request,
        host_name=args.host_name,
        port=args.port,
        access_token=args.access_token,
        limit=args.limit,
        resource_id=args.resource_id,
        subclass_depth=args.subclass_depth,
    )
    json.dump(response, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
