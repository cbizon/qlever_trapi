import io
import json
import tarfile

import zstandard as zstd
from bmt import Toolkit

from kgx_to_qlever_rdf import (
    BIOLINK_ENUM_NS,
    BIOLINK_VOCAB,
    KGXTR_REVERSE_TRAVERSAL_EDGE,
    KGXTR_TRAVERSAL_EDGE,
    KGXTR_TRAVERSAL_FROM,
    KGXTR_TRAVERSAL_TO,
    KGXTR_TRAVERSES,
    convert_archive,
    emit_enum_hierarchy,
    most_specific_categories,
    reverse_traversal_iri,
)


def write_tar_zst(path, members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, content in members.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    compressed = zstd.ZstdCompressor().compress(buffer.getvalue())
    path.write_bytes(compressed)


def test_most_specific_categories_removes_ancestors_and_keeps_peers():
    toolkit = Toolkit()
    categories = [
        "biolink:NamedThing",
        "biolink:BiologicalEntity",
        "biolink:Gene",
        "biolink:Protein",
    ]
    assert most_specific_categories(toolkit, categories) == ["biolink:Gene", "biolink:Protein"]


def test_convert_archive_emits_reification_and_hierarchies(tmp_path):
    nodes = "\n".join(
        [
            json.dumps(
                {
                    "id": "NCBIGene:1",
                    "category": ["biolink:NamedThing", "biolink:Gene"],
                    "name": "GENE1",
                }
            ),
            json.dumps(
                {
                    "id": "PR:1",
                    "category": ["biolink:NamedThing", "biolink:Protein"],
                    "name": "PROT1",
                }
            ),
        ]
    ) + "\n"
    edges = json.dumps(
        {
            "id": "urn:uuid:test-edge",
            "category": ["biolink:Association", "biolink:GeneToGeneProductRelationship"],
            "subject": "NCBIGene:1",
            "predicate": "biolink:related_to",
            "object": "PR:1",
            "knowledge_level": "knowledge_assertion",
            "agent_type": "manual_agent",
            "qualified_predicate": "biolink:causes",
            "publications": ["PMID:1"],
            "sources": [
                {
                    "id": "urn:uuid:source-1",
                    "category": ["biolink:RetrievalSource"],
                    "resource_id": "infores:test",
                    "resource_role": "primary_knowledge_source",
                }
            ],
        }
    ) + "\n"

    archive_path = tmp_path / "sample.tar.zst"
    output_path = tmp_path / "sample.nt"
    write_tar_zst(archive_path, {"nodes.jsonl": nodes, "edges.jsonl": edges})

    convert_archive(archive_path, output_path)

    triples = output_path.read_text(encoding="utf-8")

    assert "<https://identifiers.org/NCBIGene:1> <https://w3id.org/biolink/vocab/is_a> <https://w3id.org/biolink/vocab/Gene> ." in triples
    assert "<https://identifiers.org/NCBIGene:1> <https://w3id.org/biolink/vocab/related_to> <https://identifiers.org/PR:1> ." in triples
    assert "<urn:uuid:test-edge> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/1999/02/22-rdf-syntax-ns#Statement> ." in triples
    assert "<urn:uuid:test-edge> <http://www.w3.org/1999/02/22-rdf-syntax-ns#subject> <https://identifiers.org/NCBIGene:1> ." in triples
    assert "<https://w3id.org/biolink/vocab/Gene> <http://www.w3.org/2000/01/rdf-schema#subClassOf> <https://w3id.org/biolink/vocab/BiologicalEntity> ." in triples
    assert "<https://w3id.org/biolink/vocab/causes> <http://www.w3.org/2000/01/rdf-schema#subPropertyOf> <https://w3id.org/biolink/vocab/contributes_to> ." in triples
    assert f"<urn:uuid:test-edge> <{BIOLINK_VOCAB}knowledge_level> <{BIOLINK_ENUM_NS}KnowledgeLevelEnum/knowledge_assertion> ." in triples
    assert f"<{BIOLINK_ENUM_NS}ResourceRoleEnum/primary_knowledge_source> <{BIOLINK_VOCAB}is_a>" not in triples
    assert f"<urn:uuid:source-1> <{BIOLINK_VOCAB}resource_role> <{BIOLINK_ENUM_NS}ResourceRoleEnum/primary_knowledge_source> ." in triples


def test_convert_archive_writes_zstd_output(tmp_path):
    archive_path = tmp_path / "sample.tar.zst"
    output_path = tmp_path / "sample.nt.zst"
    write_tar_zst(
        archive_path,
        {
            "nodes.jsonl": json.dumps(
                {
                    "id": "NCBIGene:1",
                    "category": ["biolink:NamedThing", "biolink:Gene"],
                    "name": "GENE1",
                }
            )
            + "\n",
            "edges.jsonl": json.dumps(
                {
                    "id": "urn:uuid:test-edge",
                    "subject": "NCBIGene:1",
                    "predicate": "biolink:related_to",
                    "object": "NCBIGene:1",
                    "category": ["biolink:Association"],
                }
            )
            + "\n",
        },
    )

    convert_archive(archive_path, output_path)

    with output_path.open("rb") as compressed:
        text = zstd.ZstdDecompressor().stream_reader(compressed).read().decode("utf-8")
    assert "<https://identifiers.org/NCBIGene:1> <https://w3id.org/biolink/vocab/related_to> <https://identifiers.org/NCBIGene:1> ." in text


def test_emit_enum_hierarchy_handles_list_parent_values():
    toolkit = Toolkit()
    buffer = io.StringIO()
    seen = set()

    emit_enum_hierarchy(
        buffer,
        toolkit,
        "ClinicalApprovalStatusEnum",
        "fda_approved_for_condition",
        seen,
    )

    triples = buffer.getvalue()
    assert (
        f"<{BIOLINK_ENUM_NS}ClinicalApprovalStatusEnum/fda_approved_for_condition> "
        f"<{BIOLINK_VOCAB}is_a> "
        f"<{BIOLINK_ENUM_NS}ClinicalApprovalStatusEnum/approved_for_condition> ."
    ) in triples


def test_convert_archive_adds_reverse_traversal_aliases_when_enabled(tmp_path):
    archive_path = tmp_path / "sample.tar.zst"
    output_path = tmp_path / "sample.nt"
    write_tar_zst(
        archive_path,
        {
            "nodes.jsonl": json.dumps(
                {
                    "id": "NCBIGene:1",
                    "category": ["biolink:NamedThing", "biolink:Gene"],
                }
            )
            + "\n"
            + json.dumps(
                {
                    "id": "PR:1",
                    "category": ["biolink:NamedThing", "biolink:Protein"],
                }
            )
            + "\n",
            "edges.jsonl": json.dumps(
                {
                    "id": "urn:uuid:test-edge",
                    "subject": "NCBIGene:1",
                    "predicate": "biolink:related_to",
                    "object": "PR:1",
                    "category": ["biolink:Association"],
                }
            )
            + "\n",
        },
    )

    convert_archive(archive_path, output_path, add_reverse_traversal_edges=True)

    triples = output_path.read_text(encoding="utf-8")
    reverse_iri = reverse_traversal_iri("urn:uuid:test-edge")

    assert f"<urn:uuid:test-edge> <{KGXTR_TRAVERSAL_FROM}> <https://identifiers.org/NCBIGene:1> ." in triples
    assert f"<urn:uuid:test-edge> <{KGXTR_TRAVERSAL_TO}> <https://identifiers.org/PR:1> ." in triples
    assert f"<{reverse_iri}> <{KGXTR_TRAVERSES}> <urn:uuid:test-edge> ." in triples
    assert f"<{reverse_iri}> <{KGXTR_TRAVERSAL_FROM}> <https://identifiers.org/PR:1> ." in triples
    assert f"<{reverse_iri}> <{KGXTR_TRAVERSAL_TO}> <https://identifiers.org/NCBIGene:1> ." in triples
    assert f"<{reverse_iri}> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <{KGXTR_TRAVERSAL_EDGE}> ." in triples
    assert f"<{reverse_iri}> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <{KGXTR_REVERSE_TRAVERSAL_EDGE}> ." in triples
    assert "<urn:uuid:test-edge> <http://www.w3.org/1999/02/22-rdf-syntax-ns#subject> <https://identifiers.org/PR:1> ." not in triples
