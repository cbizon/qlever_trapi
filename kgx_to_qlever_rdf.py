#!/usr/bin/env python3
import argparse
import contextlib
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path
from typing import Any, Iterator, TextIO
from urllib.parse import quote

import zstandard as zstd
from bmt import Toolkit


BIOLINK_VOCAB = "https://w3id.org/biolink/vocab/"
IDENTIFIERS_ORG = "https://identifiers.org/"
KGX_SLOT_NS = "https://w3id.org/kgx/slot/"
KGX_NODE_NS = "https://w3id.org/kgx/node/"
BIOLINK_ENUM_NS = "https://w3id.org/biolink/enum/"
KGXTR_NS = "https://w3id.org/kgx/traversal/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDF_STATEMENT = "http://www.w3.org/1999/02/22-rdf-syntax-ns#Statement"
RDF_SUBJECT = "http://www.w3.org/1999/02/22-rdf-syntax-ns#subject"
RDF_PREDICATE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#predicate"
RDF_OBJECT = "http://www.w3.org/1999/02/22-rdf-syntax-ns#object"
RDFS_CLASS = "http://www.w3.org/2000/01/rdf-schema#Class"
RDF_PROPERTY = "http://www.w3.org/1999/02/22-rdf-syntax-ns#Property"
RDFS_SUBCLASS_OF = "http://www.w3.org/2000/01/rdf-schema#subClassOf"
RDFS_SUBPROPERTY_OF = "http://www.w3.org/2000/01/rdf-schema#subPropertyOf"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
BIOLINK_IS_A = BIOLINK_VOCAB + "is_a"
KGXTR_TRAVERSAL_EDGE = KGXTR_NS + "TraversalEdge"
KGXTR_REVERSE_TRAVERSAL_EDGE = KGXTR_NS + "ReverseTraversalEdge"
KGXTR_TRAVERSES = KGXTR_NS + "traverses"
KGXTR_TRAVERSAL_FROM = KGXTR_NS + "traversal_from"
KGXTR_TRAVERSAL_TO = KGXTR_NS + "traversal_to"

CURIE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:[^\s]+$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert a KGX tar.zst archive into N-Triples for QLever."
    )
    parser.add_argument("input", type=Path, help="Path to a KGX tar.zst archive.")
    parser.add_argument(
        "output",
        type=Path,
        help="Path to the output .nt file or compressed .nt.zst file.",
    )
    parser.add_argument(
        "--zstd-level",
        type=int,
        default=3,
        help="Compression level used when the output filename ends in `.zst`.",
    )
    parser.add_argument(
        "--add-reverse-traversal-edges",
        action="store_true",
        help="Emit reverse traversal aliases that point back to the original edge.",
    )
    return parser.parse_args()


def escape_iri(value: str) -> str:
    return value.replace("\\", "%5C").replace(">", "%3E").replace("<", "%3C")


def nt_resource(value: str) -> str:
    return f"<{escape_iri(value)}>"


def nt_literal(value: Any) -> str:
    if isinstance(value, bool):
        literal = "true" if value else "false"
        return f"\"{literal}\"^^<{quote('http://www.w3.org/2001/XMLSchema#boolean', safe=':/#')}>"
    if isinstance(value, int):
        return f"\"{value}\"^^<http://www.w3.org/2001/XMLSchema#integer>"
    if isinstance(value, float):
        return f"\"{value}\"^^<http://www.w3.org/2001/XMLSchema#double>"
    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace("\"", "\\\"")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f"\"{escaped}\""


def write_triple(handle: TextIO, subject: str, predicate: str, object_value: str) -> None:
    handle.write(f"{nt_resource(subject)} {nt_resource(predicate)} {object_value} .\n")


@contextlib.contextmanager
def open_output_text(path: Path, zstd_level: int) -> Iterator[TextIO]:
    if path.suffix == ".zst":
        with path.open("wb") as raw_handle:
            compressor = zstd.ZstdCompressor(level=zstd_level)
            with compressor.stream_writer(raw_handle) as compressed_handle:
                with io.TextIOWrapper(compressed_handle, encoding="utf-8") as text_handle:
                    yield text_handle
        return
    with path.open("w", encoding="utf-8") as text_handle:
        yield text_handle


def local_name(curie: str) -> str:
    return curie.split(":", 1)[1]


def biolink_term_to_iri(term: str) -> str:
    return BIOLINK_VOCAB + local_name(term)


def looks_like_iri_or_curie(value: str) -> bool:
    return bool(CURIE_RE.match(value))


def curie_or_iri_to_iri(value: str) -> str:
    if value.startswith(("http://", "https://", "urn:")):
        return value
    if value.startswith("biolink:"):
        return biolink_term_to_iri(value)
    return IDENTIFIERS_ORG + quote(value, safe=":/._-")


def safe_name(value: str) -> str:
    return value.replace(" ", "_")


def custom_slot_iri(key: str) -> str:
    return KGX_SLOT_NS + quote(safe_name(key), safe="._-")


def enum_value_iri(enum_name: str, value: str) -> str:
    return BIOLINK_ENUM_NS + quote(enum_name, safe="._-/") + "/" + quote(value, safe="._-")


def generated_node_iri(parent_id: str, key: str, value: Any, index: int) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {"parent_id": parent_id, "key": key, "value": value, "index": index},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return KGX_NODE_NS + digest


def reverse_traversal_iri(edge_id: str) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {"reverse_traversal_of": edge_id},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return KGXTR_NS + "reverse/" + digest


def get_slot(toolkit: Toolkit, key: str) -> Any:
    return toolkit.get_element(key)


def get_slot_iri(toolkit: Toolkit, key: str) -> str:
    slot = get_slot(toolkit, key)
    if slot is not None and getattr(slot, "slot_uri", None):
        return curie_or_iri_to_iri(slot.slot_uri)
    return custom_slot_iri(key)


def emit_label(handle: TextIO, subject_iri: str, label: str) -> None:
    write_triple(handle, subject_iri, RDFS_LABEL, nt_literal(label))


def emit_class_hierarchy(
    handle: TextIO,
    toolkit: Toolkit,
    class_term: str,
    seen_classes: set[str],
) -> None:
    if class_term in seen_classes:
        return
    seen_classes.add(class_term)
    class_iri = curie_or_iri_to_iri(class_term)
    write_triple(handle, class_iri, RDF_TYPE, nt_resource(RDFS_CLASS))
    element = toolkit.get_element(class_term)
    if element is not None:
        emit_label(handle, class_iri, element.name)
    parent = toolkit.get_parent(class_term, formatted=True)
    if parent:
        emit_class_hierarchy(handle, toolkit, parent, seen_classes)
        write_triple(handle, class_iri, RDFS_SUBCLASS_OF, nt_resource(curie_or_iri_to_iri(parent)))


def emit_predicate_hierarchy(
    handle: TextIO,
    toolkit: Toolkit,
    predicate_term: str,
    seen_predicates: set[str],
) -> None:
    if predicate_term in seen_predicates:
        return
    seen_predicates.add(predicate_term)
    predicate_iri = curie_or_iri_to_iri(predicate_term)
    write_triple(handle, predicate_iri, RDF_TYPE, nt_resource(RDF_PROPERTY))
    element = toolkit.get_element(predicate_term)
    if element is not None:
        emit_label(handle, predicate_iri, element.name)
    parent = toolkit.get_parent(predicate_term, formatted=True)
    if parent:
        emit_predicate_hierarchy(handle, toolkit, parent, seen_predicates)
        write_triple(
            handle,
            predicate_iri,
            RDFS_SUBPROPERTY_OF,
            nt_resource(curie_or_iri_to_iri(parent)),
        )


def emit_enum_hierarchy(
    handle: TextIO,
    toolkit: Toolkit,
    enum_name: str,
    value: str,
    seen_enum_values: set[tuple[str, str]],
) -> None:
    enum_key = (enum_name, value)
    if enum_key in seen_enum_values:
        return
    seen_enum_values.add(enum_key)
    value_iri = enum_value_iri(enum_name, value)
    emit_label(handle, value_iri, value)
    parents = toolkit.get_permissible_value_parent(value, enum_name)
    if not parents:
        return
    if not isinstance(parents, list):
        parents = [parents]
    for parent in parents:
        emit_enum_hierarchy(handle, toolkit, enum_name, parent, seen_enum_values)
        write_triple(handle, value_iri, BIOLINK_IS_A, nt_resource(enum_value_iri(enum_name, parent)))


def most_specific_categories(toolkit: Toolkit, categories: list[str]) -> list[str]:
    unique = list(dict.fromkeys(categories))
    specific: list[str] = []
    for category in unique:
        is_ancestor_of_other = False
        for other in unique:
            if other == category:
                continue
            other_ancestors = set(toolkit.get_ancestors(other, reflexive=False, formatted=True))
            if category in other_ancestors:
                is_ancestor_of_other = True
                break
        if not is_ancestor_of_other:
            specific.append(category)
    return specific


def emit_type_assignments(
    handle: TextIO,
    subject_iri: str,
    categories: list[str],
    toolkit: Toolkit,
    seen_classes: set[str],
) -> None:
    for category in most_specific_categories(toolkit, categories):
        emit_class_hierarchy(handle, toolkit, category, seen_classes)
        category_iri = curie_or_iri_to_iri(category)
        for predicate in (RDF_TYPE, BIOLINK_IS_A):
            write_triple(handle, subject_iri, predicate, nt_resource(category_iri))


def emit_scalar_value(
    handle: TextIO,
    toolkit: Toolkit,
    parent_iri: str,
    key: str,
    value: Any,
    seen_classes: set[str],
    seen_predicates: set[str],
    seen_enum_values: set[tuple[str, str]],
) -> None:
    predicate_iri = get_slot_iri(toolkit, key)
    if isinstance(value, str):
        slot = get_slot(toolkit, key)
        enum_name = getattr(slot, "range", None) if slot is not None else None
        if enum_name and isinstance(enum_name, str) and enum_name.endswith("Enum"):
            if toolkit.is_permissible_value_of_enum(enum_name, value):
                emit_enum_hierarchy(
                    handle,
                    toolkit,
                    enum_name,
                    value,
                    seen_enum_values,
                )
                write_triple(handle, parent_iri, predicate_iri, nt_resource(enum_value_iri(enum_name, value)))
                return
        if looks_like_iri_or_curie(value):
            object_iri = curie_or_iri_to_iri(value)
            if value.startswith("biolink:"):
                element = toolkit.get_element(value)
                if getattr(element, "class_uri", None):
                    emit_class_hierarchy(handle, toolkit, value, seen_classes)
                elif getattr(element, "slot_uri", None):
                    emit_predicate_hierarchy(handle, toolkit, value, seen_predicates)
            write_triple(handle, parent_iri, predicate_iri, nt_resource(object_iri))
            return
    write_triple(handle, parent_iri, predicate_iri, nt_literal(value))


def emit_attributes(
    handle: TextIO,
    toolkit: Toolkit,
    parent_iri: str,
    record: dict[str, Any],
    skip_keys: set[str],
    seen_classes: set[str],
    seen_predicates: set[str],
    seen_enum_values: set[tuple[str, str]],
) -> None:
    for key, value in record.items():
        if key in skip_keys or value is None:
            continue
        emit_value(
            handle,
            toolkit,
            parent_iri,
            key,
            value,
            seen_classes,
            seen_predicates,
            seen_enum_values,
        )


def emit_value(
    handle: TextIO,
    toolkit: Toolkit,
    parent_iri: str,
    key: str,
    value: Any,
    seen_classes: set[str],
    seen_predicates: set[str],
    seen_enum_values: set[tuple[str, str]],
    index: int = 0,
) -> None:
    if isinstance(value, list):
        for index, item in enumerate(value):
            emit_value(
                handle,
                toolkit,
                parent_iri,
                key,
                item,
                seen_classes,
                seen_predicates,
                seen_enum_values,
                index=index,
            )
        return
    if isinstance(value, dict):
        nested_id = value.get("id")
        nested_iri = (
            curie_or_iri_to_iri(nested_id)
            if isinstance(nested_id, str) and looks_like_iri_or_curie(nested_id)
            else generated_node_iri(parent_iri, key, value, index)
        )
        predicate_iri = get_slot_iri(toolkit, key)
        write_triple(handle, parent_iri, predicate_iri, nt_resource(nested_iri))
        categories = value.get("category") or []
        if isinstance(categories, list):
            emit_type_assignments(
                handle,
                nested_iri,
                categories,
                toolkit,
                seen_classes,
            )
        emit_attributes(
            handle,
            toolkit,
            nested_iri,
            value,
            {"id", "category"},
            seen_classes,
            seen_predicates,
            seen_enum_values,
        )
        return
    emit_scalar_value(
        handle,
        toolkit,
        parent_iri,
        key,
        value,
        seen_classes,
        seen_predicates,
        seen_enum_values,
    )


def convert_nodes(
    handle: TextIO,
    toolkit: Toolkit,
    node_stream: io.BufferedReader,
    seen_classes: set[str],
    seen_predicates: set[str],
    seen_enum_values: set[tuple[str, str]],
) -> None:
    for raw_line in node_stream:
        if not raw_line.strip():
            continue
        node = json.loads(raw_line)
        node_id = node["id"]
        node_iri = curie_or_iri_to_iri(node_id)
        categories = node.get("category") or []
        if isinstance(categories, list):
            emit_type_assignments(
                handle,
                node_iri,
                categories,
                toolkit,
                seen_classes,
            )
        emit_attributes(
            handle,
            toolkit,
            node_iri,
            node,
            {"id", "category"},
            seen_classes,
            seen_predicates,
            seen_enum_values,
        )


def convert_edges(
    handle: TextIO,
    toolkit: Toolkit,
    edge_stream: io.BufferedReader,
    seen_classes: set[str],
    seen_predicates: set[str],
    seen_enum_values: set[tuple[str, str]],
    add_reverse_traversal_edges: bool = False,
) -> None:
    for raw_line in edge_stream:
        if not raw_line.strip():
            continue
        edge = json.loads(raw_line)
        edge_iri = curie_or_iri_to_iri(edge["id"])
        subject_iri = curie_or_iri_to_iri(edge["subject"])
        predicate_iri = curie_or_iri_to_iri(edge["predicate"])
        object_iri = curie_or_iri_to_iri(edge["object"])

        if edge["predicate"].startswith("biolink:"):
            emit_predicate_hierarchy(
                handle,
                toolkit,
                edge["predicate"],
                seen_predicates,
            )

        write_triple(handle, subject_iri, predicate_iri, nt_resource(object_iri))

        for predicate, obj in (
            (RDF_TYPE, nt_resource(RDF_STATEMENT)),
            (RDF_TYPE, nt_resource(KGXTR_TRAVERSAL_EDGE)),
            (RDF_SUBJECT, nt_resource(subject_iri)),
            (RDF_PREDICATE, nt_resource(predicate_iri)),
            (RDF_OBJECT, nt_resource(object_iri)),
            (KGXTR_TRAVERSAL_FROM, nt_resource(subject_iri)),
            (KGXTR_TRAVERSAL_TO, nt_resource(object_iri)),
        ):
            write_triple(handle, edge_iri, predicate, obj)

        categories = edge.get("category") or []
        if isinstance(categories, list):
            emit_type_assignments(
                handle,
                edge_iri,
                categories,
                toolkit,
                seen_classes,
            )

        emit_attributes(
            handle,
            toolkit,
            edge_iri,
            edge,
            {"id", "subject", "predicate", "object", "category"},
            seen_classes,
            seen_predicates,
            seen_enum_values,
        )

        if add_reverse_traversal_edges:
            reverse_iri = reverse_traversal_iri(edge["id"])
            for predicate, obj in (
                (RDF_TYPE, nt_resource(KGXTR_TRAVERSAL_EDGE)),
                (RDF_TYPE, nt_resource(KGXTR_REVERSE_TRAVERSAL_EDGE)),
                (KGXTR_TRAVERSES, nt_resource(edge_iri)),
                (KGXTR_TRAVERSAL_FROM, nt_resource(object_iri)),
                (KGXTR_TRAVERSAL_TO, nt_resource(subject_iri)),
            ):
                write_triple(handle, reverse_iri, predicate, obj)


def convert_archive(
    input_path: Path,
    output_path: Path,
    zstd_level: int = 3,
    add_reverse_traversal_edges: bool = False,
) -> None:
    toolkit = Toolkit()
    seen_classes: set[str] = set()
    seen_predicates: set[str] = set()
    seen_enum_values: set[tuple[str, str]] = set()

    with input_path.open("rb") as compressed, open_output_text(output_path, zstd_level) as output_handle:
        reader = zstd.ZstdDecompressor().stream_reader(compressed)
        with reader:
            with tarfile.open(fileobj=reader, mode="r|") as archive:
                for member in archive:
                    if member.name not in {"nodes.jsonl", "edges.jsonl"}:
                        continue
                    stream = archive.extractfile(member)
                    if stream is None:
                        continue
                    if member.name == "nodes.jsonl":
                        convert_nodes(
                            output_handle,
                            toolkit,
                            stream,
                            seen_classes,
                            seen_predicates,
                            seen_enum_values,
                        )
                    elif member.name == "edges.jsonl":
                        convert_edges(
                            output_handle,
                            toolkit,
                            stream,
                            seen_classes,
                            seen_predicates,
                            seen_enum_values,
                            add_reverse_traversal_edges=add_reverse_traversal_edges,
                        )


def main() -> None:
    args = parse_args()
    convert_archive(
        args.input,
        args.output,
        zstd_level=args.zstd_level,
        add_reverse_traversal_edges=args.add_reverse_traversal_edges,
    )


if __name__ == "__main__":
    main()
