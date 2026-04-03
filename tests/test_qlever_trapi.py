import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from qlever_trapi import (
    BIOLINK_VOCAB,
    RDFS_LABEL,
    answer_trapi_request,
    build_trapi_query,
    create_trapi_http_server,
    iri_to_curie,
    normalize_trapi_request,
)


def sample_request() -> dict:
    return {
        "message": {
            "query_graph": {
                "nodes": {
                    "n0": {
                        "ids": ["CHEBI:45783"],
                        "categories": ["biolink:ChemicalEntity"],
                    },
                    "n1": {
                        "categories": ["biolink:Disease"],
                    },
                },
                "edges": {
                    "e0": {
                        "subject": "n0",
                        "object": "n1",
                        "predicates": ["biolink:treats"],
                    }
                },
            }
        }
    }


def chain_request() -> dict:
    return {
        "message": {
            "query_graph": {
                "nodes": {
                    "n0": {
                        "ids": ["CHEBI:45783"],
                        "categories": ["biolink:ChemicalEntity"],
                    },
                    "n1": {
                        "ids": ["NCBIGene:1017"],
                        "categories": ["biolink:Gene"],
                    },
                    "n2": {
                        "ids": ["MONDO:0004979"],
                        "categories": ["biolink:Disease"],
                    },
                },
                "edges": {
                    "e0": {
                        "subject": "n0",
                        "object": "n1",
                        "predicates": ["biolink:affects"],
                    },
                    "e1": {
                        "subject": "n1",
                        "object": "n2",
                        "predicates": ["biolink:related_to"],
                    },
                },
            }
        }
    }


def branch_request() -> dict:
    return {
        "message": {
            "query_graph": {
                "nodes": {
                    "gene": {
                        "ids": ["NCBIGene:1017"],
                        "categories": ["biolink:Gene"],
                    },
                    "disease": {
                        "ids": ["MONDO:0004979"],
                        "categories": ["biolink:Disease"],
                    },
                    "phenotype": {
                        "ids": ["HP:0001627"],
                        "categories": ["biolink:PhenotypicFeature"],
                    },
                },
                "edges": {
                    "e0": {
                        "subject": "gene",
                        "object": "disease",
                        "predicates": ["biolink:related_to"],
                    },
                    "e1": {
                        "subject": "gene",
                        "object": "phenotype",
                        "predicates": ["biolink:causes"],
                    },
                },
            }
        }
    }


def qualifier_request(qualifier_value: str = "activity") -> dict:
    return {
        "message": {
            "query_graph": {
                "nodes": {
                    "n0": {},
                    "n1": {
                        "ids": ["NCBIGene:283871"],
                    },
                },
                "edges": {
                    "e0": {
                        "subject": "n0",
                        "object": "n1",
                        "predicates": ["biolink:affects"],
                        "qualifier_constraints": [
                            {
                                "qualifier_set": [
                                    {
                                        "qualifier_type_id": "biolink:qualified_predicate",
                                        "qualifier_value": "biolink:causes",
                                    },
                                    {
                                        "qualifier_type_id": "biolink:object_aspect_qualifier",
                                        "qualifier_value": qualifier_value,
                                    },
                                ]
                            }
                        ],
                    }
                },
            }
        }
    }


def test_normalize_trapi_request_rejects_unreferenced_qnode() -> None:
    request = sample_request()
    request["message"]["query_graph"]["nodes"]["n2"] = {"categories": ["biolink:Gene"]}

    with pytest.raises(ValueError, match="participate in at least one qedge"):
        normalize_trapi_request(request)


def test_build_trapi_query_supports_multi_edge_shapes() -> None:
    query = build_trapi_query(normalize_trapi_request(chain_request(), subclass_depth=0), limit=25)

    assert "SELECT DISTINCT ?node_0_n0 ?node_1_n1 ?node_2_n2 ?edge_0_e0 ?edge_1_e1 ?predicate_0_e0 ?predicate_1_e1" in query
    assert query.count("a rdf:Statement") == 2
    assert "rdf:subject ?node_0_n0" in query
    assert "rdf:object ?node_1_n1" in query
    assert "rdf:subject ?node_1_n1" in query
    assert "rdf:object ?node_2_n2" in query
    assert "<https://w3id.org/biolink/vocab/affects>" in query
    assert "<https://w3id.org/biolink/vocab/related_to>" in query
    assert "subClassOf" in query
    assert "subPropertyOf" in query
    assert "LIMIT 25" in query


def test_build_trapi_query_adds_internal_subclass_patterns_for_pinned_nodes() -> None:
    query = build_trapi_query(normalize_trapi_request(sample_request(), subclass_depth=1), limit=25)

    assert "?node_2_n0_superclass" in query
    assert "?edge_1_n0_subclass_edge" in query
    assert "rdf:predicate <https://w3id.org/biolink/vocab/subclass_of>" in query
    assert "FILTER(?node_0_n0 = ?node_2_n0_superclass)" in query
    assert "UNION" in query


def test_build_trapi_query_adds_qualifier_filters() -> None:
    query = build_trapi_query(normalize_trapi_request(qualifier_request(), subclass_depth=0), limit=25)

    assert "qualifier_predicate_0_0_0" in query
    assert "qualifier_predicate_0_0_1" in query
    assert "qualified_predicate" in query
    assert "object_aspect_qualifier" in query
    assert "biolink/vocab/causes" in query
    assert "activity" in query


def test_iri_to_curie_round_trips_biolink_and_identifiers_org() -> None:
    assert iri_to_curie("https://identifiers.org/MONDO:0004979") == "MONDO:0004979"
    assert iri_to_curie(BIOLINK_VOCAB + "Gene") == "biolink:Gene"
    assert iri_to_curie("urn:uuid:test-edge") == "urn:uuid:test-edge"


@pytest.fixture()
def qlever_test_server() -> tuple[str, int]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers["Content-Length"])
            payload = self.rfile.read(length).decode("utf-8")
            params = urllib.parse.parse_qs(payload)
            query = params["query"][0]

            if "VALUES ?resource" in query:
                body = properties_tsv()
            elif "NCBIGene:283871" in query:
                body = qualified_result_tsv()
            elif "?edge_1_n0_subclass_edge" in query and "MONDO:0000001" in query:
                body = subclass_result_tsv()
            elif "HP:0001627" in query and "MONDO:0004979" in query and "NCBIGene:1017" in query:
                body = branch_result_tsv()
            elif "NCBIGene:1017" in query and "CHEBI:45783" in query and "MONDO:0004979" in query:
                body = chain_result_tsv()
            else:
                body = single_edge_result_tsv()

            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/tab-separated-values")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[0], server.server_address[1]
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def single_edge_result_tsv() -> str:
    return (
        "?node_0_n0\t?node_1_n1\t?edge_0_e0\t?predicate_0_e0\n"
        "https://identifiers.org/CHEBI:45783\t"
        "https://identifiers.org/MONDO:0004979\t"
        "urn:uuid:test-edge\t"
        "https://w3id.org/biolink/vocab/treats\n"
    )


def chain_result_tsv() -> str:
    return (
        "?node_0_n0\t?node_1_n1\t?node_2_n2\t?edge_0_e0\t?edge_1_e1\t?predicate_0_e0\t?predicate_1_e1\n"
        "https://identifiers.org/CHEBI:45783\t"
        "https://identifiers.org/NCBIGene:1017\t"
        "https://identifiers.org/MONDO:0004979\t"
        "urn:uuid:edge-affects\t"
        "urn:uuid:edge-related\t"
        "https://w3id.org/biolink/vocab/affects\t"
        "https://w3id.org/biolink/vocab/related_to\n"
    )


def branch_result_tsv() -> str:
    return (
        "?node_0_gene\t?node_1_disease\t?node_2_phenotype\t?edge_0_e0\t?edge_1_e1\t?predicate_0_e0\t?predicate_1_e1\n"
        "https://identifiers.org/NCBIGene:1017\t"
        "https://identifiers.org/MONDO:0004979\t"
        "https://identifiers.org/HP:0001627\t"
        "urn:uuid:edge-gene-disease\t"
        "urn:uuid:edge-gene-phenotype\t"
        "https://w3id.org/biolink/vocab/related_to\t"
        "https://w3id.org/biolink/vocab/causes\n"
    )


def properties_tsv() -> str:
    return (
        "?resource\t?predicate\t?value\n"
        "https://identifiers.org/CHEBI:45783\t"
        f"{RDFS_LABEL}\t\"Imatinib\"\n"
        "https://identifiers.org/CHEBI:45783\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#type\t"
        "https://w3id.org/biolink/vocab/ChemicalEntity\n"
        "https://identifiers.org/NCBIGene:1017\t"
        f"{RDFS_LABEL}\t\"CDK2\"\n"
        "https://identifiers.org/NCBIGene:1017\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#type\t"
        "https://w3id.org/biolink/vocab/Gene\n"
        "https://identifiers.org/MONDO:0004979\t"
        f"{RDFS_LABEL}\t\"asthma\"\n"
        "https://identifiers.org/MONDO:0004979\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#type\t"
        "https://w3id.org/biolink/vocab/Disease\n"
        "https://identifiers.org/HP:0001627\t"
        f"{RDFS_LABEL}\t\"Abnormal heart morphology\"\n"
        "https://identifiers.org/HP:0001627\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#type\t"
        "https://w3id.org/biolink/vocab/PhenotypicFeature\n"
        "urn:uuid:test-edge\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#subject\t"
        "https://identifiers.org/CHEBI:45783\n"
        "urn:uuid:test-edge\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#predicate\t"
        "https://w3id.org/biolink/vocab/treats\n"
        "urn:uuid:test-edge\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#object\t"
        "https://identifiers.org/MONDO:0004979\n"
        "urn:uuid:test-edge\t"
        "https://w3id.org/biolink/vocab/primary_knowledge_source\t"
        "infores:test-kp\n"
        "urn:uuid:test-edge\t"
        "https://w3id.org/kgx/slot/publications\t"
        "\"PMID:123\"\n"
        "urn:uuid:test-edge\t"
        "https://w3id.org/biolink/vocab/qualified_predicate\t"
        "https://w3id.org/biolink/vocab/causes\n"
        "urn:uuid:edge-affects\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#subject\t"
        "https://identifiers.org/CHEBI:45783\n"
        "urn:uuid:edge-affects\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#predicate\t"
        "https://w3id.org/biolink/vocab/affects\n"
        "urn:uuid:edge-affects\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#object\t"
        "https://identifiers.org/NCBIGene:1017\n"
        "urn:uuid:edge-affects\t"
        "https://w3id.org/biolink/vocab/primary_knowledge_source\t"
        "infores:test-kp\n"
        "urn:uuid:edge-related\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#subject\t"
        "https://identifiers.org/NCBIGene:1017\n"
        "urn:uuid:edge-related\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#predicate\t"
        "https://w3id.org/biolink/vocab/related_to\n"
        "urn:uuid:edge-related\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#object\t"
        "https://identifiers.org/MONDO:0004979\n"
        "urn:uuid:edge-related\t"
        "https://w3id.org/biolink/vocab/aggregator_knowledge_source\t"
        "infores:test-ara\n"
        "urn:uuid:edge-gene-disease\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#subject\t"
        "https://identifiers.org/NCBIGene:1017\n"
        "urn:uuid:edge-gene-disease\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#predicate\t"
        "https://w3id.org/biolink/vocab/related_to\n"
        "urn:uuid:edge-gene-disease\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#object\t"
        "https://identifiers.org/MONDO:0004979\n"
        "urn:uuid:edge-gene-disease\t"
        "https://w3id.org/biolink/vocab/primary_knowledge_source\t"
        "infores:test-kp\n"
        "urn:uuid:edge-gene-phenotype\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#subject\t"
        "https://identifiers.org/NCBIGene:1017\n"
        "urn:uuid:edge-gene-phenotype\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#predicate\t"
        "https://w3id.org/biolink/vocab/causes\n"
        "urn:uuid:edge-gene-phenotype\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#object\t"
        "https://identifiers.org/HP:0001627\n"
        "urn:uuid:edge-gene-phenotype\t"
        "https://w3id.org/biolink/vocab/supporting_data_source\t"
        "infores:test-source\n"
        "https://identifiers.org/MONDO:0000001\t"
        f"{RDFS_LABEL}\t\"disease\"\n"
        "https://identifiers.org/MONDO:0000001\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#type\t"
        "https://w3id.org/biolink/vocab/Disease\n"
        "https://identifiers.org/MONDO:0005148\t"
        f"{RDFS_LABEL}\t\"type 2 diabetes\"\n"
        "https://identifiers.org/MONDO:0005148\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#type\t"
        "https://w3id.org/biolink/vocab/Disease\n"
        "https://identifiers.org/HP:0012592\t"
        f"{RDFS_LABEL}\t\"Albuminuria\"\n"
        "https://identifiers.org/HP:0012592\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#type\t"
        "https://w3id.org/biolink/vocab/PhenotypicFeature\n"
        "urn:uuid:t2d-has-phenotype\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#subject\t"
        "https://identifiers.org/MONDO:0005148\n"
        "urn:uuid:t2d-has-phenotype\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#predicate\t"
        "https://w3id.org/biolink/vocab/has_phenotype\n"
        "urn:uuid:t2d-has-phenotype\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#object\t"
        "https://identifiers.org/HP:0012592\n"
        "urn:uuid:t2d-has-phenotype\t"
        "https://w3id.org/biolink/vocab/primary_knowledge_source\t"
        "infores:test-kp\n"
        "urn:uuid:t2d-isa-disease\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#subject\t"
        "https://identifiers.org/MONDO:0005148\n"
        "urn:uuid:t2d-isa-disease\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#predicate\t"
        "https://w3id.org/biolink/vocab/subclass_of\n"
        "urn:uuid:t2d-isa-disease\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#object\t"
        "https://identifiers.org/MONDO:0000001\n"
        "urn:uuid:t2d-isa-disease\t"
        "https://w3id.org/biolink/vocab/primary_knowledge_source\t"
        "infores:test-kp\n"
        "https://identifiers.org/NCBIGene:283871\t"
        f"{RDFS_LABEL}\t\"GENE283871\"\n"
        "https://identifiers.org/NCBIGene:283871\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#type\t"
        "https://w3id.org/biolink/vocab/Gene\n"
        "https://identifiers.org/PUBCHEM.COMPOUND:5460341\t"
        f"{RDFS_LABEL}\t\"Compound5460341\"\n"
        "https://identifiers.org/PUBCHEM.COMPOUND:5460341\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#type\t"
        "https://w3id.org/biolink/vocab/ChemicalEntity\n"
        "urn:uuid:qualified-edge\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#subject\t"
        "https://identifiers.org/PUBCHEM.COMPOUND:5460341\n"
        "urn:uuid:qualified-edge\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#predicate\t"
        "https://w3id.org/biolink/vocab/affects\n"
        "urn:uuid:qualified-edge\t"
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#object\t"
        "https://identifiers.org/NCBIGene:283871\n"
        "urn:uuid:qualified-edge\t"
        "https://w3id.org/biolink/vocab/primary_knowledge_source\t"
        "infores:test-kp\n"
        "urn:uuid:qualified-edge\t"
        "https://w3id.org/biolink/vocab/qualified_predicate\t"
        "https://w3id.org/biolink/vocab/causes\n"
        "urn:uuid:qualified-edge\t"
        "https://w3id.org/biolink/vocab/object_aspect_qualifier\t"
        "https://w3id.org/biolink/enum/GeneOrGeneProductOrChemicalEntityAspectEnum/activity\n"
        "urn:uuid:qualified-edge\t"
        "https://w3id.org/biolink/vocab/object_direction_qualifier\t"
        "https://w3id.org/biolink/enum/DirectionQualifierEnum/decreased\n"
    )


def subclass_result_tsv() -> str:
    return (
        "?node_0_n0\t?node_1_n1\t?node_2_n0_superclass\t?edge_0_e0\t?edge_1_n0_subclass_edge\t?predicate_0_e0\n"
        "https://identifiers.org/MONDO:0005148\t"
        "https://identifiers.org/HP:0012592\t"
        "https://identifiers.org/MONDO:0000001\t"
        "urn:uuid:t2d-has-phenotype\t"
        "urn:uuid:t2d-isa-disease\t"
        "https://w3id.org/biolink/vocab/has_phenotype\n"
    )


def qualified_result_tsv() -> str:
    return (
        "?node_0_n0\t?node_1_n1\t?edge_0_e0\t?predicate_0_e0\n"
        "https://identifiers.org/PUBCHEM.COMPOUND:5460341\t"
        "https://identifiers.org/NCBIGene:283871\t"
        "urn:uuid:qualified-edge\t"
        "https://w3id.org/biolink/vocab/affects\n"
    )


def test_answer_trapi_request_returns_message_with_knowledge_graph_and_results(
    qlever_test_server: tuple[str, int],
) -> None:
    host, port = qlever_test_server

    response = answer_trapi_request(
        sample_request(),
        host_name=host,
        port=port,
        limit=10,
        resource_id="infores:qlever-trapi-test",
        subclass_depth=0,
    )

    assert response == {
        "message": {
            "query_graph": sample_request()["message"]["query_graph"],
            "knowledge_graph": {
                "nodes": {
                    "CHEBI:45783": {
                        "categories": ["biolink:ChemicalEntity"],
                        "name": "Imatinib",
                    },
                    "MONDO:0004979": {
                        "categories": ["biolink:Disease"],
                        "name": "asthma",
                    },
                },
                "edges": {
                    "urn:uuid:test-edge": {
                        "subject": "CHEBI:45783",
                        "predicate": "biolink:treats",
                        "object": "MONDO:0004979",
                        "sources": [
                            {
                                "resource_id": "infores:test-kp",
                                "resource_role": "primary_knowledge_source",
                            }
                        ],
                        "qualifiers": [
                            {
                                "qualifier_type_id": "biolink:qualified_predicate",
                                "qualifier_value": "biolink:causes",
                            }
                        ],
                        "attributes": [
                            {
                                "attribute_type_id": "https://w3id.org/kgx/slot/publications",
                                "value": "PMID:123",
                            },
                        ],
                    }
                },
            },
            "results": [
                {
                    "node_bindings": {
                        "n0": [{"id": "CHEBI:45783"}],
                        "n1": [{"id": "MONDO:0004979"}],
                    },
                    "analyses": [
                        {
                            "resource_id": "infores:qlever-trapi-test",
                            "edge_bindings": {
                                "e0": [{"id": "urn:uuid:test-edge"}],
                            },
                        }
                    ],
                }
            ],
            "auxiliary_graphs": {},
        }
    }


def test_answer_trapi_request_supports_chain_query_graph(
    qlever_test_server: tuple[str, int],
) -> None:
    host, port = qlever_test_server

    response = answer_trapi_request(
        chain_request(),
        host_name=host,
        port=port,
        limit=10,
        resource_id="infores:qlever-trapi-test",
        subclass_depth=0,
    )

    assert response["message"]["knowledge_graph"]["nodes"]["NCBIGene:1017"] == {
        "categories": ["biolink:Gene"],
        "name": "CDK2",
    }
    assert response["message"]["knowledge_graph"]["edges"]["urn:uuid:edge-affects"]["predicate"] == "biolink:affects"
    assert response["message"]["knowledge_graph"]["edges"]["urn:uuid:edge-related"]["predicate"] == "biolink:related_to"
    assert response["message"]["results"] == [
        {
            "node_bindings": {
                "n0": [{"id": "CHEBI:45783"}],
                "n1": [{"id": "NCBIGene:1017"}],
                "n2": [{"id": "MONDO:0004979"}],
            },
            "analyses": [
                {
                    "resource_id": "infores:qlever-trapi-test",
                    "edge_bindings": {
                        "e0": [{"id": "urn:uuid:edge-affects"}],
                        "e1": [{"id": "urn:uuid:edge-related"}],
                    },
                }
            ],
        }
    ]


def test_answer_trapi_request_supports_branch_query_graph(
    qlever_test_server: tuple[str, int],
) -> None:
    host, port = qlever_test_server

    response = answer_trapi_request(
        branch_request(),
        host_name=host,
        port=port,
        limit=10,
        resource_id="infores:qlever-trapi-test",
        subclass_depth=0,
    )

    result = response["message"]["results"][0]
    assert result["node_bindings"]["gene"] == [{"id": "NCBIGene:1017"}]
    assert result["node_bindings"]["disease"] == [{"id": "MONDO:0004979"}]
    assert result["node_bindings"]["phenotype"] == [{"id": "HP:0001627"}]
    assert result["analyses"][0]["edge_bindings"] == {
        "e0": [{"id": "urn:uuid:edge-gene-disease"}],
        "e1": [{"id": "urn:uuid:edge-gene-phenotype"}],
    }


def test_answer_trapi_request_supports_endpoint_subclass_reasoning(
    qlever_test_server: tuple[str, int],
) -> None:
    host, port = qlever_test_server

    response = answer_trapi_request(
        {
            "message": {
                "query_graph": {
                    "nodes": {
                        "n0": {"ids": ["MONDO:0000001"]},
                        "n1": {},
                    },
                    "edges": {
                        "e0": {
                            "subject": "n0",
                            "object": "n1",
                        }
                    },
                }
            }
        },
        host_name=host,
        port=port,
        limit=10,
        resource_id="infores:qlever-trapi-test",
        subclass_depth=1,
    )

    result = response["message"]["results"][0]
    inferred_edge_id = result["analyses"][0]["edge_bindings"]["e0"][0]["id"]
    assert result["node_bindings"]["n0"] == [{"id": "MONDO:0000001"}]
    assert result["node_bindings"]["n1"] == [{"id": "HP:0012592"}]
    assert response["message"]["auxiliary_graphs"] == {
        "aux_" + inferred_edge_id.split(":", 1)[1]: {
            "attributes": [],
            "edges": ["urn:uuid:t2d-has-phenotype", "urn:uuid:t2d-isa-disease"],
        }
    }
    inferred_edge = response["message"]["knowledge_graph"]["edges"][inferred_edge_id]
    assert inferred_edge["subject"] == "MONDO:0000001"
    assert inferred_edge["predicate"] == "biolink:has_phenotype"
    assert inferred_edge["object"] == "HP:0012592"
    assert {"attribute_type_id": "biolink:knowledge_level", "value": "logical_entailment"} in inferred_edge["attributes"]
    assert {"attribute_type_id": "biolink:agent_type", "value": "automated_agent"} in inferred_edge["attributes"]
    assert {
        "attribute_type_id": "biolink:support_graphs",
        "value": ["aux_" + inferred_edge_id.split(":", 1)[1]],
    } in inferred_edge["attributes"]
    assert inferred_edge["sources"] == [
        {
            "resource_id": "infores:qlever-trapi-test",
            "resource_role": "primary_knowledge_source",
        }
    ]


def test_answer_trapi_request_formats_edge_qualifiers(
    qlever_test_server: tuple[str, int],
) -> None:
    host, port = qlever_test_server

    response = answer_trapi_request(
        qualifier_request(),
        host_name=host,
        port=port,
        limit=10,
        resource_id="infores:qlever-trapi-test",
        subclass_depth=0,
    )

    edge = response["message"]["knowledge_graph"]["edges"]["urn:uuid:qualified-edge"]
    assert edge["qualifiers"] == [
        {
            "qualifier_type_id": "biolink:qualified_predicate",
            "qualifier_value": "biolink:causes",
        },
        {
            "qualifier_type_id": "biolink:object_aspect_qualifier",
            "qualifier_value": "activity",
        },
        {
            "qualifier_type_id": "biolink:object_direction_qualifier",
            "qualifier_value": "decreased",
        },
    ]
    assert "attributes" not in edge


def test_answer_trapi_request_matches_qualifier_hierarchy(
    qlever_test_server: tuple[str, int],
) -> None:
    host, port = qlever_test_server

    response = answer_trapi_request(
        qualifier_request("activity_or_abundance"),
        host_name=host,
        port=port,
        limit=10,
        resource_id="infores:qlever-trapi-test",
        subclass_depth=0,
    )

    assert response["message"]["results"] == [
        {
            "node_bindings": {
                "n0": [{"id": "PUBCHEM.COMPOUND:5460341"}],
                "n1": [{"id": "NCBIGene:283871"}],
            },
            "analyses": [
                {
                    "resource_id": "infores:qlever-trapi-test",
                    "edge_bindings": {
                        "e0": [{"id": "urn:uuid:qualified-edge"}],
                    },
                }
            ],
        }
    ]


@pytest.fixture()
def trapi_service_server(qlever_test_server: tuple[str, int]) -> tuple[str, int]:
    qlever_host, qlever_port = qlever_test_server
    server = create_trapi_http_server(
        listen_host="127.0.0.1",
        listen_port=0,
        qlever_host_name=qlever_host,
        qlever_port=qlever_port,
        access_token=None,
        limit=50,
        resource_id="infores:qlever-trapi-http-test",
        subclass_depth=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[0], server.server_address[1]
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def http_json_request(
    method: str,
    url: str,
    payload: dict | None = None,
    raw_body: bytes | None = None,
) -> tuple[int, dict]:
    body = raw_body
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_http_service_health_endpoint(trapi_service_server: tuple[str, int]) -> None:
    host, port = trapi_service_server

    status, response = http_json_request("GET", f"http://{host}:{port}/health")

    assert status == 200
    assert response == {
        "description": "TRAPI service is healthy",
        "http_code": 200,
        "status": "Success",
    }


def test_http_service_query_endpoint_returns_trapi_envelope(
    trapi_service_server: tuple[str, int],
) -> None:
    host, port = trapi_service_server

    status, response = http_json_request(
        "POST",
        f"http://{host}:{port}/query",
        payload=chain_request(),
    )

    assert status == 200
    assert response["status"] == "Success"
    assert response["description"] == "Query processed successfully"
    assert response["http_code"] == 200
    assert response["message"]["knowledge_graph"]["edges"]["urn:uuid:edge-related"]["predicate"] == "biolink:related_to"
    assert response["message"]["results"][0]["analyses"][0]["resource_id"] == "infores:qlever-trapi-http-test"


def test_http_service_returns_400_for_invalid_request(trapi_service_server: tuple[str, int]) -> None:
    host, port = trapi_service_server
    bad_request = sample_request()
    bad_request["message"]["query_graph"]["nodes"]["n2"] = {
        "categories": ["biolink:Gene"],
    }

    status, response = http_json_request(
        "POST",
        f"http://{host}:{port}/query",
        payload=bad_request,
    )

    assert status == 400
    assert response["status"] == "BadRequest"
    assert "participate in at least one qedge" in response["description"]


def test_http_service_returns_404_for_unknown_path(trapi_service_server: tuple[str, int]) -> None:
    host, port = trapi_service_server

    status, response = http_json_request("GET", f"http://{host}:{port}/nope")

    assert status == 404
    assert response == {
        "description": "Unknown endpoint: /nope",
        "http_code": 404,
        "status": "NotFound",
    }
