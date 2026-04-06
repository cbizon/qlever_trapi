#!/usr/bin/env python3
import argparse
from functools import lru_cache
import hashlib
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import sys
import time
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
    run_qlever_query_with_runtime,
    strip_typed_literal,
)


RDF_TYPE = RDF_NS + "type"
RDF_STATEMENT = RDF_NS + "Statement"
RDF_PROPERTY = RDF_NS + "Property"
RDF_SUBJECT = RDF_NS + "subject"
RDF_PREDICATE = RDF_NS + "predicate"
RDF_OBJECT = RDF_NS + "object"
XSD_NS = "http://www.w3.org/2001/XMLSchema#"
RDFS_LABEL = RDFS_NS + "label"
RDFS_CLASS = RDFS_NS + "Class"
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
SOURCE_ROLE_ORDER = {
    "primary_knowledge_source": 0,
    "aggregator_knowledge_source": 1,
    "supporting_data_source": 2,
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
CAMEL_BOUNDARY_RE = re.compile(r"(?<!^)(?=[A-Z])")
TOOLKIT = Toolkit()
ALL_BIOLINK_ENUMS = tuple(TOOLKIT.view.all_enums().keys())
ATTRIBUTE_TYPE_OVERRIDES = {
    "publications": {
        "attribute_type_id": "biolink:publications",
        "value_type_id": "linkml:Uriorcurie",
    }
}
LINKED_RESOURCE_SLOTS = ("sources", "attributes")
QLEVER_DURATION_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>ns|us|ms|s)$")
GRAPH_PREDICATE_QUERY = "\n".join(
    [
        f"PREFIX rdf: <{RDF_NS}>",
        "SELECT DISTINCT ?predicate",
        "WHERE {",
        "  ?edge a rdf:Statement ;",
        "    rdf:predicate ?predicate .",
        "}",
        "",
    ]
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Translate a TRAPI query graph into a QLever query or serve it over HTTP."
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


def ensure_id_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, int) and not isinstance(value, bool):
        return [str(value)]
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a string, integer, or list of strings/integers")

    ids: list[str] = []
    for item in value:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, int) and not isinstance(item, bool):
            ids.append(str(item))
        else:
            raise ValueError(f"{field_name} must be a string, integer, or list of strings/integers")
    return ids


def ensure_constraints(value: Any, field_name: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")

    constraints: list[dict[str, Any]] = []
    for index, constraint in enumerate(value):
        if not isinstance(constraint, dict):
            raise ValueError(f"{field_name}[{index}] must be an object")
        if constraint.get("not"):
            raise ValueError(f"Unsupported attribute constraint: {constraint}")
        if "id" not in constraint or "value" not in constraint:
            raise ValueError(f"Invalid attribute constraint: {constraint}")
        operator = constraint.get("operator", "===")
        if operator not in {"==", "==="}:
            raise ValueError(f"Unsupported attribute constraint: {constraint}")
        if operator == "==" and isinstance(constraint["value"], list):
            raise ValueError(f"Unsupported attribute constraint: {constraint}")
        constraints.append(
            {
                "id": constraint["id"],
                "value": constraint["value"],
                "operator": operator,
            }
        )
    return constraints


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
    supported_fields = {"ids", "categories", "constraints", "set_interpretation", "is_set"}
    unsupported = {
        field_name
        for field_name, field_value in qnode.items()
        if field_name not in supported_fields and field_value is not None
    }
    if unsupported:
        field_list = ", ".join(sorted(unsupported))
        raise ValueError(f"qnode {qnode_id} uses unsupported fields: {field_list}")

    categories = ensure_string_list(qnode.get("categories"), f"qnode {qnode_id}.categories")
    if not categories:
        categories = ["biolink:NamedThing"]

    set_interpretation = qnode.get("set_interpretation", "BATCH")
    if set_interpretation not in {"BATCH", "ALL", "MANY"}:
        raise ValueError(f"qnode {qnode_id} has unsupported set_interpretation={set_interpretation!r}")
    if set_interpretation == "MANY":
        raise NotImplementedError("This feature is currently not implemented: set_interpretation=MANY")

    return {
        "qnode_id": qnode_id,
        "ids": ensure_id_list(qnode.get("ids"), f"qnode {qnode_id}.ids"),
        "categories": categories,
        "constraints": ensure_constraints(qnode.get("constraints"), f"qnode {qnode_id}.constraints"),
        "set_interpretation": set_interpretation,
    }


def validate_qedge(qedge_id: str, qedge: dict[str, Any], qnodes: dict[str, Any]) -> dict[str, Any]:
    subject = qedge.get("subject")
    object_ = qedge.get("object")
    if not isinstance(subject, str) or subject not in qnodes:
        raise ValueError(f"qedge {qedge_id} subject must reference an existing qnode")
    if not isinstance(object_, str) or object_ not in qnodes:
        raise ValueError(f"qedge {qedge_id} object must reference an existing qnode")

    unsupported = set(qedge) - {
        "subject",
        "object",
        "predicates",
        "predicate",
        "relation",
        "knowledge_type",
        "qualifier_constraints",
        "attribute_constraints",
    }
    if unsupported:
        field_list = ", ".join(sorted(unsupported))
        raise ValueError(f"qedge {qedge_id} uses unsupported fields: {field_list}")

    knowledge_type = qedge.get("knowledge_type")
    if knowledge_type not in (None, "lookup"):
        raise ValueError(f"qedge {qedge_id} knowledge_type={knowledge_type!r} is not supported")
    relation = qedge.get("relation")
    if relation is not None:
        raise ValueError(f"qedge {qedge_id} relation={relation!r} is not supported")

    return {
        "qedge_id": qedge_id,
        "subject": subject,
        "object": object_,
        "predicates": ensure_string_list(qedge.get("predicates", qedge.get("predicate")), f"qedge {qedge_id}.predicates"),
        "qualifier_constraints": ensure_qualifier_constraints(
            qedge.get("qualifier_constraints"),
            f"qedge {qedge_id}.qualifier_constraints",
        ),
        "attribute_constraints": ensure_constraints(
            qedge.get("attribute_constraints"),
            f"qedge {qedge_id}.attribute_constraints",
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
    if not isinstance(qnodes, dict):
        raise ValueError("query_graph.nodes must be an object")
    if not isinstance(qedges, dict):
        raise ValueError("query_graph.edges must be an object")
    if not qnodes and not qedges:
        return {
            "message": message,
            "query_graph": query_graph,
            "original_qnodes": {},
            "original_qedges": {},
            "qnodes": {},
            "qedges": {},
            "referenced_qnodes": set(),
            "orphan_qnodes": set(),
        }

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
    orphan_qnodes = {qnode_id for qnode_id in normalized_qnodes if qnode_id not in referenced_qnodes}

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
        "referenced_qnodes": referenced_qnodes,
        "orphan_qnodes": orphan_qnodes,
    }


def values_clause(variable: str, iris: list[str]) -> str:
    return f"VALUES {variable} {{ {' '.join(iri_term(iri) for iri in iris)} }}"


def safe_name(value: str) -> str:
    return value.replace(" ", "_")


def space_case(value: str | list[str]) -> str | list[str]:
    if isinstance(value, list):
        return [space_case(item) for item in value]
    if not isinstance(value, str):
        raise ValueError(f"Unsupported value for space_case: {type(value).__name__}")
    stripped = value.removeprefix("biolink:")
    if "_" in stripped:
        words = stripped.replace("_", " ")
    else:
        words = CAMEL_BOUNDARY_RE.sub(" ", stripped)
    return " ".join(words.split()).lower()


def snake_case(value: str | list[str]) -> str | list[str]:
    if isinstance(value, list):
        return [snake_case(item) for item in value]
    if not isinstance(value, str):
        raise ValueError(f"Unsupported value for snake_case: {type(value).__name__}")
    return str(space_case(value)).replace(" ", "_")


def pascal_case(value: str | list[str]) -> str | list[str]:
    if isinstance(value, list):
        return [pascal_case(item) for item in value]
    if not isinstance(value, str):
        raise ValueError(f"Unsupported value for pascal_case: {type(value).__name__}")
    return "".join(part.capitalize() for part in str(space_case(value)).split())


def custom_slot_iri(key: str) -> str:
    return KGX_SLOT_NS + quote(safe_name(key), safe="._-")


def enum_value_iri(enum_name: str, value: str) -> str:
    return BIOLINK_ENUM_NS + quote(enum_name, safe="._-/") + "/" + quote(value, safe="._-")


def sparql_string_literal(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def sparql_literal(value: Any) -> str:
    if isinstance(value, bool):
        literal = "true" if value else "false"
        return f'"{literal}"^^<{XSD_NS}boolean>'
    if isinstance(value, int):
        return f'"{value}"^^<{XSD_NS}integer>'
    if isinstance(value, float):
        return f'"{value}"^^<{XSD_NS}double>'
    if isinstance(value, str):
        return sparql_string_literal(value)
    raise ValueError(f"Unsupported property type: {type(value).__name__}.")


def slot_iri(key: str) -> str:
    if key.startswith(("http://", "https://", "urn:")):
        return key
    if key.startswith("biolink:"):
        return curie_to_iri(key)
    element = TOOLKIT.get_element(key)
    if element is not None and getattr(element, "slot_uri", None):
        return curie_to_iri(element.slot_uri)
    return custom_slot_iri(key)


def looks_like_iri_or_curie(value: str) -> bool:
    return ":" in value and not value.startswith(" ")


def constraint_value_term(constraint_id: str, value: Any) -> str:
    if isinstance(value, list):
        raise ValueError(f"Unsupported property type: {type(value).__name__}.")
    slot_name = constraint_id.removeprefix("biolink:")
    element = TOOLKIT.get_element(slot_name)
    enum_name = getattr(element, "range", None) if element is not None else None

    if isinstance(value, str):
        if isinstance(enum_name, str) and enum_name.endswith("Enum") and TOOLKIT.is_permissible_value_of_enum(enum_name, value):
            return iri_term(enum_value_iri(enum_name, value))
        if looks_like_iri_or_curie(value):
            return iri_term(curie_to_iri(value))
    return sparql_literal(value)


@lru_cache(maxsize=None)
def descendant_predicates(predicate: str) -> tuple[str, ...]:
    element = TOOLKIT.get_element(space_case(predicate))
    if element is None:
        raise ValueError(f"Invalid predicate in query: {predicate}")
    descendants = TOOLKIT.get_descendants(space_case(predicate))
    return tuple(
        f"biolink:{snake_case(descendant)}"
        for descendant in descendants
        if (
            TOOLKIT.get_element(descendant).annotations.get("canonical_predicate", False)
            or ("symmetric" in TOOLKIT.get_element(descendant) and TOOLKIT.get_element(descendant).symmetric)
        )
    )


@lru_cache(maxsize=None)
def graph_predicates(host_name: str, port: int, access_token: str | None) -> frozenset[str]:
    rows = rows_from_result(
        run_qlever_query(
            host_name,
            port,
            GRAPH_PREDICATE_QUERY,
            access_token=access_token,
        )
    )
    return frozenset(
        iri_to_curie(row["?predicate"][1:-1] if row["?predicate"].startswith("<") and row["?predicate"].endswith(">") else row["?predicate"])
        for row in rows
    )


def predicate_match_modes(
    predicates: list[str],
    available_graph_predicates: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    if "biolink:related_to" in predicates:
        predicates = []
    queried_predicates = list(predicates)
    inverse_predicates: list[str] = []
    symmetric = True
    for predicate in predicates:
        element = TOOLKIT.get_element(space_case(predicate))
        if element is None:
            raise ValueError(f"Invalid predicate in query: {predicate}")
        inverse_predicate = getattr(element, "inverse", None)
        if inverse_predicate is not None:
            inverse_predicates.append(f"biolink:{snake_case(inverse_predicate)}")
        if getattr(element, "symmetric", False):
            inverse_predicates.append(predicate)
        else:
            symmetric = False

    if not predicates:
        return [
            {"reverse": False, "predicates": []},
            {"reverse": True, "predicates": []},
        ]

    forward_predicates = dedupe(
        predicate
        for requested_predicate in predicates
        for predicate in descendant_predicates(requested_predicate)
    )
    reverse_predicates = dedupe(
        predicate
        for requested_predicate in inverse_predicates
        for predicate in descendant_predicates(requested_predicate)
    )
    if available_graph_predicates is not None:
        forward_predicates = [predicate for predicate in forward_predicates if predicate in available_graph_predicates]
        reverse_predicates = [predicate for predicate in reverse_predicates if predicate in available_graph_predicates]

    if queried_predicates and not forward_predicates and not reverse_predicates:
        raise ValueError(
            "A query was made with the following predicates, but none of them or their descendants are in the graph queried: "
            + ", ".join(queried_predicates)
        )

    if symmetric:
        allowed = dedupe(forward_predicates + reverse_predicates)
        return [
            {"reverse": False, "predicates": allowed},
            {"reverse": True, "predicates": allowed},
        ]

    if forward_predicates and reverse_predicates:
        return [
            {"reverse": False, "predicates": forward_predicates},
            {"reverse": True, "predicates": reverse_predicates},
        ]
    if reverse_predicates:
        return [{"reverse": True, "predicates": reverse_predicates}]
    return [{"reverse": False, "predicates": forward_predicates}]


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
        synthetic_qnode = {
            "qnode_id": superclass_qnode_id,
            "ids": list(qnode["ids"]),
            "categories": list(qnode["categories"]),
            "_superclass": True,
            "_original_qnode_id": qnode_id,
        }
        if len(qnode["ids"]) == 1:
            synthetic_qnode["_constant_id"] = qnode["ids"][0]
        synthetic_qnodes[superclass_qnode_id] = synthetic_qnode
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


def qnode_constant_id(qnode: dict[str, Any]) -> str | None:
    constant_id = qnode.get("_constant_id")
    return constant_id if isinstance(constant_id, str) else None


def qnode_constant_term(qnode: dict[str, Any]) -> str:
    constant_id = qnode_constant_id(qnode)
    if constant_id is None:
        raise ValueError(f"QNode {qnode['qnode_id']} does not have a constant binding")
    return iri_term(curie_to_iri(constant_id))


def qnode_result_id(qnode: dict[str, Any], row: dict[str, str]) -> str:
    constant_id = qnode_constant_id(qnode)
    if constant_id is not None:
        return constant_id
    return iri_to_curie(row[qnode_binding_var(qnode)])


def qnode_result_iri(qnode: dict[str, Any], row: dict[str, str]) -> str:
    constant_id = qnode_constant_id(qnode)
    if constant_id is not None:
        return curie_to_iri(constant_id)
    return row[qnode_binding_var(qnode)]


def qedge_binding_var(qedge: dict[str, Any]) -> str:
    return f"?edge_{qedge['index']}_{safe_var_suffix(qedge['qedge_id'])}"


def qedge_predicate_var(qedge: dict[str, Any]) -> str:
    return f"?predicate_{qedge['index']}_{safe_var_suffix(qedge['qedge_id'])}"


def qedge_qualifier_predicate_var(qedge: dict[str, Any], constraint_index: int, filter_index: int) -> str:
    return f"?qualifier_predicate_{qedge['index']}_{constraint_index}_{filter_index}"


def qedge_qualifier_value_var(qedge: dict[str, Any], constraint_index: int, filter_index: int) -> str:
    return f"?qualifier_value_{qedge['index']}_{constraint_index}_{filter_index}"


def qedge_orientation_var(qedge: dict[str, Any]) -> str:
    return f"?orientation_{qedge['index']}_{safe_var_suffix(qedge['qedge_id'])}"


def qnode_existence_var(qnode: dict[str, Any]) -> str:
    return f"?node_type_{qnode['index']}_{safe_var_suffix(qnode['qnode_id'])}"


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


def decode_enum_value(value: str) -> Any:
    if value.startswith(BIOLINK_ENUM_NS):
        return unquote(value.rsplit("/", 1)[1])
    return decode_typed_literal(value)


def decode_qualifier_value(value: str) -> Any:
    return decode_enum_value(value)


def build_constraint_lines(
    variable: str,
    constraints: list[dict[str, Any]],
    indent: str = "  ",
) -> list[str]:
    lines: list[str] = []
    for constraint in constraints:
        lines.append(
            f"{indent}{variable} <{slot_iri(constraint['id'])}> {constraint_value_term(constraint['id'], constraint['value'])} ."
        )
    return lines


def bind_orphan_qnode(lines: list[str], qnode: dict[str, Any]) -> None:
    lines.append(
        f"  {qnode_binding_var(qnode)} <{RDF_TYPE}> {qnode_existence_var(qnode)} ."
    )
    lines.append(
        f"  FILTER({qnode_existence_var(qnode)} != <{RDF_STATEMENT}> && {qnode_existence_var(qnode)} != <{RDF_PROPERTY}> && {qnode_existence_var(qnode)} != <{RDFS_CLASS}>)"
    )


def append_node_filters(lines: list[str], qnode: dict[str, Any]) -> None:
    constant_term = qnode_constant_term(qnode) if qnode_constant_id(qnode) is not None else None
    variable = constant_term or qnode_binding_var(qnode)
    if constant_term is None:
        ids = [curie_to_iri(value) for value in qnode["ids"]]
        if ids:
            lines.append(f"  {values_clause(variable, ids)}")

    categories = [curie_to_iri(value) for value in qnode["categories"]]
    if categories:
        category_var = qnode_category_var(qnode)
        lines.append(f"  {values_clause(category_var, categories)}")
        lines.append(f"  {variable} <{RDF_TYPE}>/<{RDFS_SUBCLASS_OF}>* {category_var} .")
    lines.extend(build_constraint_lines(variable, qnode.get("constraints", [])))

def build_qualifier_set_lines(
    qedge: dict[str, Any],
    qualifier_set: list[dict[str, str]],
    constraint_index: int,
    indent: str = "    ",
) -> list[str]:
    lines: list[str] = []
    for filter_index, qualifier_filter in enumerate(qualifier_set):
        predicate_var = qedge_qualifier_predicate_var(qedge, constraint_index, filter_index)
        value_var = qedge_qualifier_value_var(qedge, constraint_index, filter_index)
        lines.append(
            f"{indent}{values_clause(predicate_var, qualifier_predicate_iris(qualifier_filter['qualifier_type_id']))}"
        )
        lines.append(
            f"{indent}{values_clause(value_var, qualifier_value_iris(qualifier_filter['qualifier_type_id'], qualifier_filter['qualifier_value']))}"
        )
        lines.append(f"{indent}{qedge_binding_var(qedge)} {predicate_var} {value_var} .")
    return lines


def build_qualifier_constraint_union_lines(
    qedge: dict[str, Any],
    indent: str = "    ",
) -> list[str]:
    branches: list[list[str]] = []
    for constraint_index, constraint in enumerate(qedge.get("qualifier_constraints", [])):
        qualifier_set = constraint.get("qualifier_set", [])
        if not qualifier_set:
            continue
        branch_lines = [f"{indent}{{"]
        branch_lines.extend(
            build_qualifier_set_lines(
                qedge,
                qualifier_set,
                constraint_index,
                indent=indent + "  ",
            )
        )
        branch_lines.append(f"{indent}}}")
        branches.append(branch_lines)

    if not branches:
        return []

    lines: list[str] = []
    for index, branch_lines in enumerate(branches):
        if index:
            lines.append(f"{indent}UNION")
        lines.extend(branch_lines)
    return lines


def build_qedge_mode_lines(
    normalized_request: dict[str, Any],
    qedge: dict[str, Any],
    mode: dict[str, Any],
) -> list[str]:
    subject_var = qnode_binding_var(normalized_request["qnodes"][qedge["subject"]])
    object_var = qnode_binding_var(normalized_request["qnodes"][qedge["object"]])
    predicate_var = qedge_predicate_var(qedge)
    orientation = "reverse" if mode["reverse"] else "forward"
    statement_subject_var = object_var if mode["reverse"] else subject_var
    statement_object_var = subject_var if mode["reverse"] else object_var

    lines = [
        "  {",
        f"    {qedge_binding_var(qedge)} a rdf:Statement ;",
        f"      rdf:subject {statement_subject_var} ;",
        f"      rdf:predicate {predicate_var} ;",
        f"      rdf:object {statement_object_var} .",
    ]
    predicate_iris = [curie_to_iri(value) for value in mode["predicates"]]
    if predicate_iris:
        lines.append(f"    {values_clause(predicate_var, predicate_iris)}")
    lines.extend(build_constraint_lines(qedge_binding_var(qedge), qedge.get("attribute_constraints", []), indent="    "))
    lines.extend(build_qualifier_constraint_union_lines(qedge, indent="    "))
    lines.append(f'    BIND("{orientation}" AS {qedge_orientation_var(qedge)})')
    lines.append("  }")
    return lines


def build_qedge_union_lines(
    normalized_request: dict[str, Any],
    qedge: dict[str, Any],
    available_graph_predicates: frozenset[str] | None = None,
) -> list[str]:
    modes = predicate_match_modes(qedge["predicates"], available_graph_predicates=available_graph_predicates)
    lines: list[str] = []
    for mode_index, mode in enumerate(modes):
        if mode_index:
            lines.append("  UNION")
        lines.extend(build_qedge_mode_lines(normalized_request, qedge, mode))
    return lines


def build_trapi_query(
    normalized_request: dict[str, Any],
    limit: int | None = None,
    available_graph_predicates: frozenset[str] | None = None,
) -> str:
    qnode_vars = [
        qnode_binding_var(qnode)
        for qnode in normalized_request["qnodes"].values()
        if qnode_constant_id(qnode) is None
    ]
    qedge_vars = [qedge_binding_var(qedge) for qedge in normalized_request["qedges"].values()]
    predicate_vars = [
        qedge_predicate_var(qedge)
        for qedge in normalized_request["qedges"].values()
        if not qedge.get("_subclass", False)
    ]
    orientation_vars = [
        qedge_orientation_var(qedge)
        for qedge in normalized_request["qedges"].values()
        if not qedge.get("_subclass", False)
    ]

    lines = [
        f"PREFIX rdf: <{RDF_NS}>",
        "",
        f"SELECT DISTINCT {' '.join(qnode_vars + qedge_vars + predicate_vars + orientation_vars)}",
        "WHERE {",
    ]

    for qedge in normalized_request["qedges"].values():
        if qedge.get("_subclass", False):
            lines.extend(build_subclass_union_lines(normalized_request, qedge))
        else:
            lines.extend(
                build_qedge_union_lines(
                    normalized_request,
                    qedge,
                    available_graph_predicates=available_graph_predicates,
                )
            )

    for qnode_id in normalized_request["orphan_qnodes"]:
        bind_orphan_qnode(lines, normalized_request["qnodes"][qnode_id])

    for qnode in normalized_request["qnodes"].values():
        append_node_filters(lines, qnode)

    lines.extend(
        [
            "}",
        ]
    )
    if limit is not None:
        lines.append(f"LIMIT {limit}")
    return "\n".join(lines) + "\n"


def build_subclass_union_lines(normalized_request: dict[str, Any], qedge: dict[str, Any]) -> list[str]:
    actual_var = qnode_binding_var(normalized_request["qnodes"][qedge["subject"]])
    superclass_qnode = normalized_request["qnodes"][qedge["object"]]
    superclass_var = (
        qnode_constant_term(superclass_qnode)
        if qnode_constant_id(superclass_qnode) is not None
        else qnode_binding_var(superclass_qnode)
    )
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
        if "^^<" in value and value.endswith(">"):
            text, datatype = value.split("^^<", 1)
            datatype = datatype[:-1]
            if datatype.endswith("#boolean"):
                return text == "true"
            if datatype.endswith("#integer"):
                return int(text)
            if datatype.endswith("#double"):
                return float(text)
            return text
        if value.startswith(BIOLINK_ENUM_NS):
            return unquote(value.rsplit("/", 1)[1])
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


def predicate_original_attribute_name(predicate: str) -> str:
    if predicate.startswith(KGX_SLOT_NS):
        return unquote(predicate[len(KGX_SLOT_NS) :])
    if predicate.startswith(BIOLINK_VOCAB):
        return unquote(predicate[len(BIOLINK_VOCAB) :])
    return iri_to_curie(predicate)


def attribute_element(predicate: str) -> Any:
    return TOOLKIT.get_element(predicate_original_attribute_name(predicate).replace("_", " "))


def unique_values(values: list[Any]) -> list[Any]:
    seen: set[str] = set()
    unique: list[Any] = []
    for value in values:
        key = json.dumps(value, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        unique.append(value)
    return unique


def json_literal_text(value: str) -> str:
    return unescape_literal(strip_typed_literal(value))


def parse_preformatted_attribute_literal(value: str) -> dict[str, Any]:
    payload = json.loads(json_literal_text(value))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid preformatted attribute payload: {value}")
    return payload


def is_resource_reference(value: str) -> bool:
    return (
        not value.startswith(('"', "{", "["))
        and (
            value.startswith(("http://", "https://", "urn:"))
            or (":" in value and value[0].isalnum())
        )
    )


def build_attribute_metadata(predicate: str) -> dict[str, Any]:
    original_attribute_name = predicate_original_attribute_name(predicate)
    if original_attribute_name in ATTRIBUTE_TYPE_OVERRIDES:
        return {
            "original_attribute_name": original_attribute_name,
            **ATTRIBUTE_TYPE_OVERRIDES[original_attribute_name],
        }

    element = attribute_element(predicate)
    if element is None:
        return {
            "original_attribute_name": original_attribute_name,
            "attribute_type_id": "biolink:Attribute",
        }

    metadata = {"original_attribute_name": original_attribute_name}
    if getattr(element, "slot_uri", None):
        metadata["attribute_type_id"] = element.slot_uri
    elif getattr(element, "class_uri", None):
        metadata["attribute_type_id"] = element.class_uri
    else:
        metadata["attribute_type_id"] = "biolink:Attribute"

    range_name = getattr(element, "range", None)
    if not isinstance(range_name, str):
        return metadata
    if range_name == "uriorcurie":
        value_type_id = "linkml:Uriorcurie"
    else:
        range_element = TOOLKIT.get_element(range_name)
        if range_element is None:
            value_type_id = None
        elif getattr(range_element, "uri", None):
            value_type_id = range_element.uri
        elif getattr(range_element, "class_uri", None):
            value_type_id = range_element.class_uri
        elif getattr(range_element, "slot_uri", None):
            value_type_id = range_element.slot_uri
        else:
            value_type_id = None
    if value_type_id and value_type_id != metadata["attribute_type_id"]:
        metadata["value_type_id"] = value_type_id
    return metadata


def build_trapi_attributes(
    properties: list[dict[str, str]],
    exclude_predicates: set[str],
    all_properties: dict[str, list[dict[str, str]]],
) -> list[dict[str, Any]]:
    attributes = preformatted_attributes_from_properties(properties, all_properties)
    grouped_values: dict[str, list[Any]] = {}
    for prop in properties:
        predicate = prop["predicate"]
        if predicate in exclude_predicates:
            continue
        grouped_values.setdefault(predicate, []).append(decode_typed_literal(prop["value"]))

    for predicate, values in grouped_values.items():
        deduped_values = unique_values(values)
        element = attribute_element(predicate)
        if len(deduped_values) > 1 or (element is not None and getattr(element, "multivalued", False)):
            value: Any = deduped_values
        else:
            value = deduped_values[0]
        attributes.append(
            {
                **build_attribute_metadata(predicate),
                "value": value,
            }
        )
    return attributes


def preformatted_attribute_from_resource(
    resource: str,
    properties: dict[str, list[dict[str, str]]],
    seen_resources: set[str] | None = None,
) -> dict[str, Any]:
    if seen_resources is None:
        seen_resources = set()
    if resource in seen_resources:
        raise ValueError(f"Cyclic attribute resource graph at {resource}")
    seen_resources = set(seen_resources)
    seen_resources.add(resource)

    payload: dict[str, Any] = {}
    grouped_values: dict[str, list[Any]] = {}
    for prop in properties.get(resource, []):
        key = predicate_original_attribute_name(prop["predicate"])
        if key == "attributes":
            nested_attribute = (
                preformatted_attribute_from_resource(prop["value"], properties, seen_resources)
                if is_resource_reference(prop["value"])
                else parse_preformatted_attribute_literal(prop["value"])
            )
            payload.setdefault("attributes", []).append(nested_attribute)
            continue
        grouped_values.setdefault(key, []).append(decode_typed_literal(prop["value"]))

    for key, values in grouped_values.items():
        deduped = unique_values(values)
        payload[key] = deduped if len(deduped) > 1 else deduped[0]
    return payload


def preformatted_attributes_from_properties(
    properties: list[dict[str, str]],
    all_properties: dict[str, list[dict[str, str]]],
) -> list[dict[str, Any]]:
    attributes: list[dict[str, Any]] = []
    attributes_predicate = slot_iri("attributes")
    for prop in properties:
        if prop["predicate"] != attributes_predicate:
            continue
        attributes.append(
            preformatted_attribute_from_resource(prop["value"], all_properties)
            if is_resource_reference(prop["value"])
            else parse_preformatted_attribute_literal(prop["value"])
        )
    return unique_values(attributes)


def source_references_from_properties(properties: list[dict[str, str]]) -> list[str]:
    source_predicate = slot_iri("sources")
    return dedupe(
        [
            prop["value"]
            for prop in properties
            if prop["predicate"] == source_predicate and not prop["value"].startswith('"')
        ]
    )


def dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for source in sources:
        key = (
            source["resource_id"],
            source["resource_role"],
            tuple(source.get("upstream_resource_ids", [])),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(source)
    return deduped


def sort_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        dedupe_sources(sources),
        key=lambda source: (
            SOURCE_ROLE_ORDER.get(source["resource_role"], 99),
            source["resource_id"],
            tuple(source.get("upstream_resource_ids", [])),
        ),
    )


def nested_sources_from_properties(
    edge_properties: list[dict[str, str]],
    properties: dict[str, list[dict[str, str]]],
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    resource_id_predicate = slot_iri("resource_id")
    resource_role_predicate = slot_iri("resource_role")
    upstream_resource_ids_predicate = slot_iri("upstream_resource_ids")

    for source_resource in source_references_from_properties(edge_properties):
        source_properties = properties.get(source_resource, [])
        by_predicate = resource_values_by_predicate(source_properties)
        resource_id_values = by_predicate.get(resource_id_predicate, [])
        resource_role_values = by_predicate.get(resource_role_predicate, [])
        if not resource_id_values or not resource_role_values:
            continue
        source: dict[str, Any] = {
            "resource_id": str(decode_typed_literal(resource_id_values[0])),
            "resource_role": str(decode_enum_value(resource_role_values[0])),
        }
        upstream_resource_ids = unique_values(
            [str(decode_typed_literal(value)) for value in by_predicate.get(upstream_resource_ids_predicate, [])]
        )
        if upstream_resource_ids:
            source["upstream_resource_ids"] = upstream_resource_ids
        sources.append(source)
    return dedupe_sources(sources)


def legacy_sources_from_properties(properties: list[dict[str, str]]) -> list[dict[str, Any]]:
    sources: list[dict[str, str]] = []
    grouped_resource_ids = {
        role: unique_values(
            [str(decode_typed_literal(prop["value"])) for prop in properties if prop["predicate"] == predicate]
        )
        for predicate, role in SOURCE_ROLE_BY_PREDICATE.items()
    }
    primary_resource_ids = grouped_resource_ids["primary_knowledge_source"]
    for resource_id in primary_resource_ids:
        sources.append({"resource_id": resource_id, "resource_role": "primary_knowledge_source"})
    for role in ("aggregator_knowledge_source", "supporting_data_source"):
        for resource_id in grouped_resource_ids[role]:
            source: dict[str, Any] = {"resource_id": resource_id, "resource_role": role}
            if primary_resource_ids:
                source["upstream_resource_ids"] = primary_resource_ids
            sources.append(source)
    return dedupe_sources(sources)


def terminal_source_ids(sources: list[dict[str, Any]]) -> list[str]:
    upstream_resource_ids = {
        upstream_resource_id
        for source in sources
        for upstream_resource_id in source.get("upstream_resource_ids", [])
    }
    terminal_ids: list[str] = []
    seen: set[str] = set()
    for source in sources:
        resource_id = source["resource_id"]
        if resource_id in upstream_resource_ids or resource_id in seen:
            continue
        seen.add(resource_id)
        terminal_ids.append(resource_id)
    return terminal_ids


def build_sources(
    edge_properties: list[dict[str, str]],
    properties: dict[str, list[dict[str, str]]],
    resource_id: str,
) -> list[dict[str, Any]]:
    sources = nested_sources_from_properties(edge_properties, properties)
    if not sources:
        sources = legacy_sources_from_properties(edge_properties)
    sources = sort_sources(sources)

    if not any(source["resource_role"] == "primary_knowledge_source" for source in sources):
        return [
            {
                "resource_id": resource_id,
                "resource_role": "primary_knowledge_source",
            }
        ]

    if any(source["resource_id"] == resource_id for source in sources):
        return sources

    sources.append(
        {
            "resource_id": resource_id,
            "resource_role": "aggregator_knowledge_source",
            "upstream_resource_ids": terminal_source_ids(sources),
        }
    )
    return dedupe_sources(sources)


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
    return sorted(
        qualifiers,
        key=lambda qualifier: (
            0 if qualifier["qualifier_type_id"] == "biolink:qualified_predicate" else 1,
            qualifier["qualifier_type_id"],
            json.dumps(qualifier["qualifier_value"], sort_keys=True, default=str),
        ),
    )


def qualifier_predicates_in_properties(properties: list[dict[str, str]]) -> set[str]:
    return {prop["predicate"] for prop in properties if qualifier_type_id_from_predicate(prop["predicate"]) is not None}


def resource_values_by_predicate(properties: list[dict[str, str]]) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for prop in properties:
        values.setdefault(prop["predicate"], []).append(prop["value"])
    return values


def edge_payload_from_properties(
    edge_iri: str,
    properties: dict[str, list[dict[str, str]]],
    resource_id: str,
) -> dict[str, Any]:
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
    sources = build_sources(edge_props, properties, resource_id)
    if sources:
        edge_payload["sources"] = sources
    qualifiers = build_qualifiers(edge_props)
    if qualifiers:
        edge_payload["qualifiers"] = qualifiers
    edge_attributes = build_trapi_attributes(
        edge_props,
        exclude_predicates=(
            STRUCTURAL_EDGE_PREDICATES
            | set(SOURCE_ROLE_BY_PREDICATE)
            | qualifier_predicates_in_properties(edge_props)
            | {slot_iri("sources"), slot_iri("attributes")}
        ),
        all_properties=properties,
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
            exclude_predicates={RDF_TYPE, *NAME_PREDICATES, slot_iri("attributes")},
            all_properties=properties,
        )
        if node_attributes:
            node_payload["attributes"] = node_attributes
        nodes[node_key] = node_payload
    return nodes


def build_knowledge_graph_edges(
    edge_iris: list[str],
    properties: dict[str, list[dict[str, str]]],
    resource_id: str,
) -> dict[str, Any]:
    edges: dict[str, Any] = {}
    for edge_iri in edge_iris:
        edge_key = iri_to_curie(edge_iri)
        if edge_key in edges:
            continue
        edges[edge_key] = edge_payload_from_properties(edge_iri, properties, resource_id)
    return edges


def support_edge_ids_from_row(qedge: dict[str, Any], row: dict[str, str]) -> list[str]:
    value = strip_typed_literal(row.get(qedge_binding_var(qedge), ""))
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


def build_result_key(
    node_bindings: dict[str, list[dict[str, Any]]],
    qnodes_with_set_interpretation_all: set[str],
) -> str:
    return json.dumps(
        {
            qnode_id: binding[0]["id"] if binding else None
            for qnode_id, binding in node_bindings.items()
            if qnode_id not in qnodes_with_set_interpretation_all
        },
        sort_keys=True,
    )


def build_resource_properties_query(
    resources: list[str],
    include_structural: bool = False,
    predicate_iris: list[str] | tuple[str, ...] | None = None,
) -> str:
    values = " ".join(iri_term(resource) for resource in resources)
    predicate_values_clause = ""
    if predicate_iris:
        predicate_values_clause = f"  {values_clause('?predicate', list(predicate_iris))}\n"
    filter_clause = (
        ""
        if include_structural or predicate_iris
        else f"  FILTER (?predicate NOT IN (<{RDF_NS}subject>, <{RDF_NS}predicate>, <{RDF_NS}object>))\n"
    )
    return f"""SELECT ?resource ?predicate ?value
WHERE {{
  VALUES ?resource {{ {values} }}
{predicate_values_clause}  ?resource ?predicate ?value .
{filter_clause}}}
"""


def build_knowledge_graph(
    node_iris: list[str],
    edge_iris: list[str],
    properties: dict[str, list[dict[str, str]]],
    resource_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        build_knowledge_graph_nodes(node_iris, properties),
        build_knowledge_graph_edges(edge_iris, properties, resource_id),
    )


def build_meta_knowledge_graph_edge_query() -> str:
    return "\n".join(
        [
            "PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>",
            "SELECT DISTINCT ?subject_category ?predicate ?object_category",
            "WHERE {",
            "  ?edge a rdf:Statement ;",
            "    rdf:subject ?subject ;",
            "    rdf:predicate ?predicate ;",
            "    rdf:object ?object .",
            "  ?subject rdf:type ?subject_category .",
            "  ?object rdf:type ?object_category .",
            f"  FILTER(?subject_category != <{RDF_STATEMENT}>)",
            f"  FILTER(?object_category != <{RDF_STATEMENT}>)",
            "}",
            "ORDER BY ?subject_category ?predicate ?object_category",
            "",
        ]
    )


def build_meta_knowledge_graph_node_query() -> str:
    return "\n".join(
        [
            "PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>",
            "SELECT DISTINCT ?category ?node",
            "WHERE {",
            "  ?node rdf:type ?category .",
            f"  FILTER(?category != <{RDF_STATEMENT}>)",
            "}",
            "ORDER BY ?category ?node",
            "",
        ]
    )


def curie_prefix(value: str) -> str | None:
    curie = iri_to_curie(value)
    if "://" in curie or curie.startswith("urn:") or ":" not in curie:
        return None
    return curie.split(":", 1)[0]


def meta_edge_key(subject: str, predicate: str, object_: str) -> str:
    digest = hashlib.sha256(f"{subject}|{predicate}|{object_}".encode("utf-8")).hexdigest()
    return digest[:16]


def elapsed_ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


def parse_duration_ms(value: str | int | float | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return round(value)

    text = value.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return round(float(text))

    match = QLEVER_DURATION_RE.fullmatch(text)
    if match is None:
        raise ValueError(f"Unsupported QLever duration format: {value}")

    magnitude = float(match["value"])
    unit = match["unit"]
    factor = {
        "ns": 0.000001,
        "us": 0.001,
        "ms": 1,
        "s": 1000,
    }[unit]
    return round(magnitude * factor)


def qlever_runtime_timing(result: dict[str, Any]) -> dict[str, int | None] | None:
    runtime_information = result.get("json", {}).get("runtimeInformation")
    if runtime_information is None:
        return None

    time_payload = runtime_information.get("time", {})
    meta_payload = runtime_information.get("meta", {})
    execution_tree = runtime_information.get("query_execution_tree", {})
    planning_ms = parse_duration_ms(meta_payload.get("time_query_planning"))
    compute_result_ms = parse_duration_ms(time_payload.get("computeResult"))
    if compute_result_ms is None:
        compute_result_ms = (
            parse_duration_ms(execution_tree.get("original_total_time"))
            or parse_duration_ms(execution_tree.get("total_time"))
        )
    total_ms = parse_duration_ms(time_payload.get("total"))
    if total_ms is None:
        if planning_ms is not None and compute_result_ms is not None:
            total_ms = planning_ms + compute_result_ms
        else:
            total_ms = compute_result_ms

    return {
        "planning_ms": planning_ms,
        "compute_result_ms": compute_result_ms,
        "total_ms": total_ms,
    }


def answer_meta_knowledge_graph_request(
    host_name: str = "localhost",
    port: int = 8888,
    access_token: str | None = None,
) -> dict[str, Any]:
    edge_result = run_qlever_query(
        host_name,
        port,
        build_meta_knowledge_graph_edge_query(),
        access_token=access_token,
    )
    node_result = run_qlever_query(
        host_name,
        port,
        build_meta_knowledge_graph_node_query(),
        access_token=access_token,
    )

    nodes: dict[str, dict[str, Any]] = {}
    for row in rows_from_result(node_result):
        category = iri_to_curie(row["?category"])
        node_payload = nodes.setdefault(category, {"id_prefixes": []})
        prefix = curie_prefix(row["?node"])
        if prefix and prefix not in node_payload["id_prefixes"]:
            node_payload["id_prefixes"].append(prefix)
    for node_payload in nodes.values():
        node_payload["id_prefixes"].sort()

    edges: dict[str, dict[str, str]] = {}
    for row in rows_from_result(edge_result):
        subject = iri_to_curie(row["?subject_category"])
        predicate = iri_to_curie(row["?predicate"])
        object_ = iri_to_curie(row["?object_category"])
        edges[meta_edge_key(subject, predicate, object_)] = {
            "subject": subject,
            "predicate": predicate,
            "object": object_,
        }

    return {
        "nodes": dict(sorted(nodes.items())),
        "edges": dict(sorted(edges.items())),
    }


def qnodes_with_superclass_nodes(normalized_request: dict[str, Any]) -> set[str]:
    return {
        qnode_id
        for qnode_id in normalized_request["original_qnodes"]
        if f"{qnode_id}_superclass" in normalized_request["qnodes"]
    }


def qnodes_with_set_interpretation_all(normalized_request: dict[str, Any]) -> set[str]:
    return {
        qnode_id
        for qnode_id, qnode in normalized_request["original_qnodes"].items()
        if qnode.get("set_interpretation", "BATCH") == "ALL"
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


def qedge_orientation_from_row(qedge: dict[str, Any], row: dict[str, str]) -> str:
    return strip_typed_literal(row.get(qedge_orientation_var(qedge), '"forward"')) or "forward"


def merge_binding_lists(existing_bindings: list[dict[str, Any]], new_bindings: list[dict[str, Any]]) -> None:
    existing_ids = {binding["id"] for binding in existing_bindings}
    for binding in new_bindings:
        if binding["id"] not in existing_ids:
            existing_bindings.append(binding)
            existing_ids.add(binding["id"])


def build_results(
    normalized_request: dict[str, Any],
    rows: list[dict[str, str]],
    resource_id: str,
    knowledge_graph_edges: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    aux_graphs: dict[str, Any] = {}
    attached_subclass_edges = qedges_with_attached_subclass_edges(normalized_request)
    set_interpretation_all_qnodes = qnodes_with_set_interpretation_all(normalized_request)

    for row in rows:
        node_bindings: dict[str, list[dict[str, Any]]] = {}
        for qnode_id, qnode in normalized_request["original_qnodes"].items():
            actual_node_id = qnode_result_id(normalized_request["qnodes"][qnode_id], row)
            superclass_qnode_id = f"{qnode_id}_superclass"
            if qnode_id in set_interpretation_all_qnodes:
                binding_node_id = actual_node_id
            elif superclass_qnode_id in normalized_request["qnodes"]:
                superclass_node_id = qnode_result_id(normalized_request["qnodes"][superclass_qnode_id], row)
                binding_node_id = actual_node_id if actual_node_id == superclass_node_id else superclass_node_id
            else:
                binding_node_id = actual_node_id
            node_bindings[qnode_id] = [{"id": binding_node_id}]

        edge_bindings: dict[str, list[dict[str, Any]]] = {}
        for qedge_id, qedge in normalized_request["original_qedges"].items():
            internal_qedge = normalized_request["qedges"][qedge_id]
            real_edge_iri = row[qedge_binding_var(internal_qedge)]
            real_edge_id = iri_to_curie(real_edge_iri)
            orientation = qedge_orientation_from_row(internal_qedge, row)

            support_edge_ids: list[str] = []
            superclass_node_ids: dict[str, str] = {}
            for endpoint, subclass_qedge_id, superclass_qnode_id in attached_subclass_edges.get(qedge_id, []):
                subclass_qedge = normalized_request["qedges"][subclass_qedge_id]
                subclass_support_iris = support_edge_ids_from_row(subclass_qedge, row)
                if subclass_support_iris:
                    support_edge_ids.extend(iri_to_curie(edge_iri) for edge_iri in subclass_support_iris)
                    superclass_node_ids[endpoint] = qnode_result_id(
                        normalized_request["qnodes"][superclass_qnode_id],
                        row,
                    )

            if support_edge_ids:
                if orientation == "reverse":
                    resolved_superclass_node_ids = {
                        ("object" if endpoint == "subject" else "subject"): node_id
                        for endpoint, node_id in superclass_node_ids.items()
                    }
                else:
                    resolved_superclass_node_ids = superclass_node_ids

                inferred_id = inferred_edge_id(real_edge_id, support_edge_ids, resolved_superclass_node_ids)
                aux_id = auxiliary_graph_id(inferred_id)
                if aux_id not in aux_graphs:
                    aux_graphs[aux_id] = {
                        "edges": [real_edge_id] + support_edge_ids,
                        "attributes": [],
                    }
                if inferred_id not in knowledge_graph_edges:
                    real_edge = knowledge_graph_edges[real_edge_id]
                    inferred_edge: dict[str, Any] = {
                        "subject": resolved_superclass_node_ids.get("subject", real_edge["subject"]),
                        "predicate": real_edge["predicate"],
                        "object": resolved_superclass_node_ids.get("object", real_edge["object"]),
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

        result_key = build_result_key(node_bindings, set_interpretation_all_qnodes)
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

        existing_node_bindings = results[result_key]["node_bindings"]
        for qnode_id in set_interpretation_all_qnodes:
            merge_binding_lists(existing_node_bindings.setdefault(qnode_id, []), node_bindings[qnode_id])

        existing_edge_bindings = results[result_key]["analyses"][0]["edge_bindings"]
        for qedge_id, binding_list in edge_bindings.items():
            merge_binding_lists(existing_edge_bindings.setdefault(qedge_id, []), binding_list)

    return list(results.values()), knowledge_graph_edges, aux_graphs


def merge_resource_properties(
    base_properties: dict[str, list[dict[str, str]]],
    additional_properties: dict[str, list[dict[str, str]]],
) -> dict[str, list[dict[str, str]]]:
    merged = {resource: list(resource_properties) for resource, resource_properties in base_properties.items()}
    for resource, resource_properties in additional_properties.items():
        merged.setdefault(resource, []).extend(resource_properties)
    return merged


def linked_resource_predicates() -> set[str]:
    return {slot_iri(slot_name) for slot_name in LINKED_RESOURCE_SLOTS}


def linked_resources_from_property_map(
    properties: dict[str, list[dict[str, str]]],
    predicates: set[str] | None = None,
) -> list[str]:
    predicates = predicates or linked_resource_predicates()
    return dedupe(
        [
            prop["value"]
            for resource_properties in properties.values()
            for prop in resource_properties
            if prop["predicate"] in predicates and is_resource_reference(prop["value"])
        ]
    )


def source_resource_predicates() -> tuple[str, str, str]:
    return (
        slot_iri("resource_id"),
        slot_iri("resource_role"),
        slot_iri("upstream_resource_ids"),
    )


def expand_linked_resource_properties(
    host_name: str,
    port: int,
    properties: dict[str, list[dict[str, str]]],
    access_token: str | None = None,
    timing: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, str]]]:
    total_start = time.perf_counter()
    expanded = {resource: list(resource_properties) for resource, resource_properties in properties.items()}
    seen_resources = set(expanded)
    source_predicate = slot_iri("sources")
    attributes_predicate = slot_iri("attributes")
    source_frontier = [
        resource
        for resource in linked_resources_from_property_map(expanded, {source_predicate})
        if resource not in seen_resources
    ]
    attribute_frontier = [
        resource
        for resource in linked_resources_from_property_map(expanded, {attributes_predicate})
        if resource not in seen_resources
    ]
    iteration_timings: list[dict[str, Any]] = []

    while source_frontier or attribute_frontier:
        attribute_frontier = dedupe(attribute_frontier)
        source_frontier = [
            resource
            for resource in dedupe(source_frontier)
            if resource not in attribute_frontier
        ]
        iteration_start = time.perf_counter()
        iteration_timing: dict[str, Any] = {}
        fetched: dict[str, list[dict[str, str]]] = {}
        if source_frontier:
            source_query_timing: dict[str, Any] = {}
            fetched = merge_resource_properties(
                fetched,
                fetch_properties_for_resources(
                    host_name,
                    port,
                    source_frontier,
                    access_token=access_token,
                    include_structural=False,
                    predicate_iris=source_resource_predicates(),
                    timing=source_query_timing,
                ),
            )
            iteration_timing["source_query"] = source_query_timing
        if attribute_frontier:
            attribute_query_timing: dict[str, Any] = {}
            fetched = merge_resource_properties(
                fetched,
                fetch_properties_for_resources(
                    host_name,
                    port,
                    attribute_frontier,
                    access_token=access_token,
                    include_structural=False,
                    timing=attribute_query_timing,
                ),
            )
            iteration_timing["attribute_query"] = attribute_query_timing
        expanded = merge_resource_properties(expanded, fetched)
        seen_resources.update(source_frontier)
        seen_resources.update(attribute_frontier)
        iteration_timing.update(
            {
                "resource_count": len(source_frontier) + len(attribute_frontier),
                "source_resource_count": len(source_frontier),
                "attribute_resource_count": len(attribute_frontier),
                "fetched_resource_count": len(fetched),
                "total_ms": elapsed_ms(iteration_start),
            }
        )
        iteration_timings.append(iteration_timing)
        source_frontier = [
            resource
            for resource in linked_resources_from_property_map(fetched, {source_predicate})
            if resource not in seen_resources
        ]
        attribute_frontier = [
            resource
            for resource in linked_resources_from_property_map(fetched, {attributes_predicate})
            if resource not in seen_resources
        ]

    if timing is not None:
        timing.update(
            {
                "seed_resource_count": len(properties),
                "expanded_resource_count": len(expanded),
                "iteration_count": len(iteration_timings),
                "iterations": iteration_timings,
                "total_ms": elapsed_ms(total_start),
            }
        )

    return expanded


def fetch_properties_for_resources(
    host_name: str,
    port: int,
    resources: list[str],
    access_token: str | None = None,
    include_structural: bool = False,
    predicate_iris: list[str] | tuple[str, ...] | None = None,
    timing: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, str]]]:
    total_start = time.perf_counter()
    if not resources:
        if timing is not None:
            timing.update(
                {
                    "resource_count": 0,
                    "wall_ms": 0,
                    "json_parse_ms": 0,
                    "row_parse_ms": 0,
                    "property_count": 0,
                    "fetched_resource_count": 0,
                    "qlever": None,
                    "total_ms": 0,
                }
            )
        return {}
    result = run_qlever_query_with_runtime(
        host_name,
        port,
        build_resource_properties_query(
            resources,
            include_structural=include_structural,
            predicate_iris=predicate_iris,
        ),
        access_token=access_token,
    )
    row_parse_start = time.perf_counter()
    rows = rows_from_result(result)
    rows.sort(key=lambda row: (row["?resource"], row["?predicate"], row["?value"]))
    row_parse_ms = elapsed_ms(row_parse_start)
    properties: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        properties.setdefault(row["?resource"], []).append(
            {
                "predicate": row["?predicate"],
                "value": row["?value"],
            }
        )
    if timing is not None:
        timing.update(
            {
                "resource_count": len(resources),
                "wall_ms": result["elapsed_ms"],
                "json_parse_ms": result["json_parse_ms"],
                "row_parse_ms": row_parse_ms,
                "property_count": sum(len(resource_properties) for resource_properties in properties.values()),
                "fetched_resource_count": len(properties),
                "qlever": qlever_runtime_timing(result),
                "total_ms": elapsed_ms(total_start),
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
    total_start = time.perf_counter()

    normalize_start = time.perf_counter()
    normalized = normalize_trapi_request(request, subclass_depth=subclass_depth)
    normalize_request_ms = elapsed_ms(normalize_start)
    if not normalized["qnodes"] and not normalized["qedges"]:
        message = {
            "query_graph": normalized["query_graph"],
            "knowledge_graph": {
                "nodes": {},
                "edges": {},
            },
            "results": [],
            "auxiliary_graphs": {},
        }
        return {
            "message": message,
            "timing": {
                "total_ms": elapsed_ms(total_start),
                "trapi_to_sparql": {
                    "normalize_request_ms": normalize_request_ms,
                    "build_sparql_ms": 0,
                    "total_ms": normalize_request_ms,
                },
                "primary_query": None,
                "property_fetch": {
                    "resource_count": 0,
                    "node_resource_count": 0,
                    "edge_resource_count": 0,
                    "initial_query": None,
                    "linked_expansion": {
                        "seed_resource_count": 0,
                        "expanded_resource_count": 0,
                        "iteration_count": 0,
                        "iterations": [],
                        "total_ms": 0,
                    },
                    "total_ms": 0,
                },
                "trapi_response": {
                    "collect_resources_ms": 0,
                    "build_knowledge_graph_ms": 0,
                    "build_results_ms": 0,
                    "total_ms": 0,
                },
                "counts": {
                    "query_row_count": 0,
                    "node_count": 0,
                    "edge_count": 0,
                    "result_count": 0,
                    "auxiliary_graph_count": 0,
                },
            },
        }

    build_query_start = time.perf_counter()
    available_graph_predicates = None
    if any(qedge.get("predicates") for qedge in normalized["qedges"].values() if not qedge.get("_subclass", False)):
        available_graph_predicates = graph_predicates(host_name, port, access_token)
    query = build_trapi_query(normalized, limit=limit, available_graph_predicates=available_graph_predicates)
    build_sparql_ms = elapsed_ms(build_query_start)

    result = run_qlever_query_with_runtime(
        host_name,
        port,
        query,
        access_token=access_token,
    )
    row_parse_start = time.perf_counter()
    rows = rows_from_result(result)
    row_parse_ms = elapsed_ms(row_parse_start)

    collect_resources_start = time.perf_counter()
    node_resources: list[str] = []
    edge_resources: list[str] = []
    for row in rows:
        for qnode in normalized["qnodes"].values():
            node_resources.append(qnode_result_iri(qnode, row))
        for qedge in normalized["qedges"].values():
            if qedge.get("_subclass", False):
                edge_resources.extend(support_edge_ids_from_row(qedge, row))
            else:
                edge_resources.append(row[qedge_binding_var(qedge)])
    node_resources = dedupe(node_resources)
    edge_resources = dedupe(edge_resources)
    resources = dedupe(node_resources + edge_resources)
    collect_resources_ms = elapsed_ms(collect_resources_start)

    initial_property_query_timing: dict[str, Any] = {}
    linked_expansion_timing: dict[str, Any] = {}
    property_fetch_start = time.perf_counter()
    properties = fetch_properties_for_resources(
        host_name,
        port,
        resources,
        access_token=access_token,
        include_structural=True,
        timing=initial_property_query_timing,
    )
    properties = expand_linked_resource_properties(
        host_name,
        port,
        properties,
        access_token=access_token,
        timing=linked_expansion_timing,
    )
    property_fetch_total_ms = elapsed_ms(property_fetch_start)

    build_knowledge_graph_start = time.perf_counter()
    nodes, edges = build_knowledge_graph(node_resources, edge_resources, properties, resource_id)
    build_knowledge_graph_ms = elapsed_ms(build_knowledge_graph_start)
    build_results_start = time.perf_counter()
    results, edges, auxiliary_graphs = build_results(normalized, rows, resource_id, edges)
    build_results_ms = elapsed_ms(build_results_start)
    trapi_response_total_ms = collect_resources_ms + build_knowledge_graph_ms + build_results_ms

    message = {
        "query_graph": normalized["query_graph"],
        "knowledge_graph": {
            "nodes": nodes,
            "edges": edges,
        },
        "results": results,
        "auxiliary_graphs": auxiliary_graphs,
    }
    timing = {
        "total_ms": elapsed_ms(total_start),
        "trapi_to_sparql": {
            "normalize_request_ms": normalize_request_ms,
            "build_sparql_ms": build_sparql_ms,
            "total_ms": normalize_request_ms + build_sparql_ms,
        },
        "primary_query": {
            "wall_ms": result["elapsed_ms"],
            "json_parse_ms": result["json_parse_ms"],
            "row_parse_ms": row_parse_ms,
            "row_count": len(rows),
            "qlever": qlever_runtime_timing(result),
        },
        "property_fetch": {
            "resource_count": len(resources),
            "node_resource_count": len(node_resources),
            "edge_resource_count": len(edge_resources),
            "initial_query": initial_property_query_timing,
            "linked_expansion": linked_expansion_timing,
            "total_ms": property_fetch_total_ms,
        },
        "trapi_response": {
            "collect_resources_ms": collect_resources_ms,
            "build_knowledge_graph_ms": build_knowledge_graph_ms,
            "build_results_ms": build_results_ms,
            "total_ms": trapi_response_total_ms,
        },
        "counts": {
            "query_row_count": len(rows),
            "node_count": len(nodes),
            "edge_count": len(edges),
            "result_count": len(results),
            "auxiliary_graph_count": len(auxiliary_graphs),
        },
    }

    return {
        "message": message,
        "timing": timing,
    }


def response_envelope(
    status: str,
    description: str,
    http_code: int,
    message: dict[str, Any] | None = None,
    timing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "status": status,
        "description": description,
        "http_code": http_code,
    }
    if message is not None:
        body["message"] = message
    if timing is not None:
        body["timing"] = timing
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
            except (ValueError, NotImplementedError) as exc:
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
                    timing=response["timing"],
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
