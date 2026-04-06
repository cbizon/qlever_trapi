import json
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient
import pytest

from qlever_trapi import (
    BIOLINK_VOCAB,
    KGX_SLOT_NS,
    RDFS_LABEL,
    answer_trapi_request,
    build_trapi_query,
    create_trapi_http_server,
    iri_to_curie,
    normalize_trapi_request,
)
from qlever_trapi_fastapi import create_fastapi_app


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


def inverse_predicate_request() -> dict:
    return {
        "message": {
            "query_graph": {
                "nodes": {
                    "n0": {"ids": ["HP:0012592"]},
                    "n1": {"ids": ["MONDO:0005148"]},
                },
                "edges": {
                    "e0": {
                        "subject": "n0",
                        "object": "n1",
                        "predicates": ["biolink:phenotype_of"],
                    }
                },
            }
        }
    }


def symmetric_predicate_request() -> dict:
    return {
        "message": {
            "query_graph": {
                "nodes": {
                    "n0": {"ids": ["NCBIGene:1017"]},
                    "n1": {"ids": ["MONDO:0005148"]},
                },
                "edges": {
                    "e0": {
                        "subject": "n0",
                        "object": "n1",
                        "predicates": ["biolink:genetically_associated_with"],
                    }
                },
            }
        }
    }


def node_constraint_request() -> dict:
    return {
        "message": {
            "query_graph": {
                "nodes": {
                    "n0": {
                        "categories": ["biolink:Gene"],
                        "constraints": [{"id": "chromosome", "value": "17"}],
                    }
                },
                "edges": {},
            }
        }
    }


def edge_constraint_request() -> dict:
    request = sample_request()
    request["message"]["query_graph"]["edges"]["e0"]["attribute_constraints"] = [
        {"id": "fda_approved", "value": True}
    ]
    return request


def all_nodes_request() -> dict:
    return {
        "message": {
            "query_graph": {
                "nodes": {
                    "n0": {
                        "ids": ["MONDO:0004979", "MONDO:0005148"],
                        "categories": ["biolink:Disease"],
                        "set_interpretation": "ALL",
                    }
                },
                "edges": {},
            }
        }
    }


def no_predicate_request() -> dict:
    request = sample_request()
    del request["message"]["query_graph"]["edges"]["e0"]["predicates"]
    return request


def related_to_request() -> dict:
    request = sample_request()
    request["message"]["query_graph"]["edges"]["e0"]["predicates"] = ["biolink:related_to"]
    return request


def numeric_constraint_request() -> dict:
    return {
        "message": {
            "query_graph": {
                "nodes": {
                    "n0": {
                        "categories": ["biolink:Gene"],
                        "constraints": [{"id": "length", "value": 277}],
                    }
                },
                "edges": {},
            }
        }
    }


def publications_request() -> dict:
    return {
        "message": {
            "query_graph": {
                "nodes": {
                    "n0": {"ids": ["NCBIGene:836"]},
                    "n1": {"ids": ["NCBIGene:841"]},
                },
                "edges": {
                    "e0": {
                        "subject": "n0",
                        "object": "n1",
                    }
                },
            }
        }
    }


def p_value_request() -> dict:
    return {
        "message": {
            "query_graph": {
                "nodes": {
                    "n0": {"ids": ["MONDO:0005148"]},
                    "n1": {"ids": ["NCBIGene:841"]},
                },
                "edges": {
                    "e0": {
                        "subject": "n0",
                        "object": "n1",
                    }
                },
            }
        }
    }


def generic_attribute_request() -> dict:
    return {
        "message": {
            "query_graph": {
                "nodes": {
                    "n0": {"ids": ["NCBIGene:836"]},
                    "n1": {"ids": ["MONDO:0005148"]},
                },
                "edges": {
                    "e0": {
                        "subject": "n0",
                        "object": "n1",
                    }
                },
            }
        }
    }


def json_attribute_request() -> dict:
    return {
        "message": {
            "query_graph": {
                "nodes": {
                    "n0": {"ids": ["NCBIGene:672"]},
                    "n1": {"ids": ["MONDO:0004993"]},
                },
                "edges": {
                    "e0": {
                        "subject": "n0",
                        "object": "n1",
                    }
                },
            }
        }
    }


def missing_primary_source_request() -> dict:
    return {
        "message": {
            "query_graph": {
                "nodes": {
                    "n0": {"ids": ["MESH:D014612"]},
                    "n1": {
                        "ids": ["MONDO:0005260"],
                        "categories": ["biolink:Disease"],
                    },
                },
                "edges": {
                    "e0": {
                        "subject": "n0",
                        "object": "n1",
                        "predicates": ["biolink:causes"],
                    }
                },
            }
        }
    }


def multi_qualifier_request() -> dict:
    request = qualifier_request()
    request["message"]["query_graph"]["edges"]["e0"]["qualifier_constraints"] = [
        {
            "qualifier_set": [
                {
                    "qualifier_type_id": "biolink:object_aspect_qualifier",
                    "qualifier_value": "activity",
                }
            ]
        },
        {
            "qualifier_set": [
                {
                    "qualifier_type_id": "biolink:qualified_predicate",
                    "qualifier_value": "biolink:causes",
                }
            ]
        },
    ]
    return request


def empty_qualifier_request() -> dict:
    request = qualifier_request()
    request["message"]["query_graph"]["edges"]["e0"]["qualifier_constraints"] = [
        {"qualifier_set": []}
    ]
    return request


def inverse_subclass_request() -> dict:
    return {
        "message": {
            "query_graph": {
                "nodes": {
                    "n0": {
                        "ids": ["HP:0000118"],
                        "categories": ["biolink:PhenotypicFeature"],
                    },
                    "n1": {},
                },
                "edges": {
                    "e0": {
                        "subject": "n0",
                        "object": "n1",
                        "predicates": ["biolink:phenotype_of"],
                    }
                },
            }
        }
    }


def node_only_root_request() -> dict:
    return {
        "message": {
            "query_graph": {
                "nodes": {
                    "n0": {"ids": ["MONDO:0000001"]},
                },
                "edges": {},
            }
        }
    }


def empty_request() -> dict:
    return {
        "message": {
            "query_graph": {
                "nodes": {},
                "edges": {},
            }
        }
    }


def test_normalize_trapi_request_allows_orphan_qnode() -> None:
    request = sample_request()
    request["message"]["query_graph"]["nodes"]["n2"] = {"categories": ["biolink:Gene"]}

    normalized = normalize_trapi_request(request)

    assert normalized["orphan_qnodes"] == {"n2"}
    assert "n2" in normalized["original_qnodes"]


def test_build_trapi_query_supports_multi_edge_shapes() -> None:
    query = build_trapi_query(normalize_trapi_request(chain_request(), subclass_depth=0), limit=25)

    assert "SELECT DISTINCT ?node_0_n0 ?node_1_n1 ?node_2_n2 ?edge_0_e0 ?edge_1_e1 ?predicate_0_e0 ?predicate_1_e1 ?orientation_0_e0 ?orientation_1_e1" in query
    assert query.count("a rdf:Statement") >= 2
    assert "rdf:subject ?node_0_n0" in query
    assert "rdf:object ?node_1_n1" in query
    assert "rdf:subject ?node_1_n1" in query
    assert "rdf:object ?node_2_n2" in query
    assert "<https://w3id.org/biolink/vocab/affects>" in query
    assert "VALUES ?predicate_0_e0" in query
    assert "VALUES ?predicate_1_e1" not in query
    assert "subPropertyOf" not in query
    assert "UNION" in query
    assert "ORDER BY" not in query
    assert "LIMIT 25" in query


def test_build_trapi_query_adds_internal_subclass_patterns_for_pinned_nodes() -> None:
    query = build_trapi_query(normalize_trapi_request(sample_request(), subclass_depth=1), limit=25)

    assert "?edge_1_n0_subclass_edge" in query
    assert "rdf:predicate <https://w3id.org/biolink/vocab/subclass_of>" in query
    assert "FILTER(?node_0_n0 = <https://identifiers.org/CHEBI:45783>)" in query
    assert "<https://identifiers.org/CHEBI:45783> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type>/<http://www.w3.org/2000/01/rdf-schema#subClassOf>* ?node_category_2_n0_superclass ." in query
    assert "UNION" in query


def test_build_trapi_query_adds_qualifier_filters() -> None:
    query = build_trapi_query(normalize_trapi_request(qualifier_request(), subclass_depth=0), limit=25)

    assert "qualifier_predicate_0_0_0" in query
    assert "qualifier_predicate_0_0_1" in query
    assert "qualified_predicate" in query
    assert "object_aspect_qualifier" in query
    assert "biolink/vocab/causes" in query
    assert "activity" in query


def test_build_trapi_query_prunes_predicates_not_present_in_graph() -> None:
    query = build_trapi_query(
        normalize_trapi_request(chain_request(), subclass_depth=0),
        limit=25,
        available_graph_predicates=frozenset({"biolink:affects"}),
    )

    assert "<https://w3id.org/biolink/vocab/affects>" in query
    assert "<https://w3id.org/biolink/vocab/ameliorates_condition>" not in query
    assert "VALUES ?predicate_0_e0 { <https://w3id.org/biolink/vocab/affects> }" in query


def test_build_trapi_query_supports_node_only_orphan_queries() -> None:
    query = build_trapi_query(normalize_trapi_request(node_constraint_request(), subclass_depth=1), limit=10)

    assert "a rdf:Statement" not in query
    assert "?node_0_n0 <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> ?node_type_0_n0 ." in query
    assert "chromosome" in query


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

            if "SELECT DISTINCT ?predicate" in query and "rdf:predicate ?predicate" in query:
                body = graph_predicates_tsv()
            elif "?subject_category ?predicate ?object_category" in query:
                body = meta_knowledge_graph_edge_tsv()
            elif "?category ?node" in query:
                body = meta_knowledge_graph_node_tsv()
            elif "VALUES ?resource" in query:
                body = properties_tsv(
                    query_resource_values(query),
                    query_predicate_values(query),
                )
            elif "?edge_1_n0_subclass_edge" in query and "HP:0000118" in query:
                body = inverse_subclass_result_tsv()
            elif "?edge_1_n0_subclass_edge" in query and "MONDO:0000001" in query:
                body = subclass_result_tsv()
            elif "NCBIGene:283871" in query and "?qualifier_predicate_0_1_0" in query:
                body = multi_qualified_result_tsv()
            elif "NCBIGene:283871" in query and "abundance" in query and "activity_or_abundance" not in query:
                body = empty_edge_result_tsv()
            elif "NCBIGene:283871" in query:
                body = qualified_result_tsv()
            elif "NCBIGene:672" in query and "MONDO:0004993" in query:
                body = json_attribute_result_tsv()
            elif "NCBIGene:836" in query and "NCBIGene:841" in query:
                body = publications_edge_result_tsv()
            elif "MESH:D014612" in query and "MONDO:0005260" in query:
                body = missing_primary_source_result_tsv()
            elif "MONDO:0005148" in query and "NCBIGene:841" in query:
                body = p_value_result_tsv()
            elif "NCBIGene:836" in query and "MONDO:0005148" in query and "rdf:Statement" in query:
                body = generic_attribute_result_tsv()
            elif "genetically_associated_with" in query and "NCBIGene:1017" in query and "MONDO:0005148" in query:
                body = symmetric_result_tsv()
            elif "HP:0012592" in query and "MONDO:0005148" in query:
                body = inverse_result_tsv()
            elif "length" in query and "rdf:Statement" not in query:
                body = numeric_constraint_result_tsv()
            elif "chromosome" in query and "rdf:Statement" not in query:
                body = node_constraint_result_tsv()
            elif "MONDO:0000001" in query and "rdf:Statement" not in query:
                body = node_only_root_result_tsv()
            elif "MONDO:0004979" in query and "MONDO:0005148" in query and "rdf:Statement" not in query:
                body = all_nodes_result_tsv()
            elif "HP:0001627" in query and "MONDO:0004979" in query and "NCBIGene:1017" in query:
                body = branch_result_tsv()
            elif "NCBIGene:1017" in query and "CHEBI:45783" in query and "MONDO:0004979" in query:
                body = chain_result_tsv()
            else:
                body = single_edge_result_tsv()

            if self.headers.get("Accept") == "application/qlever-results+json":
                response_body = qlever_results_json(query, body)
                content_type = "application/qlever-results+json"
            else:
                response_body = body
                content_type = "text/tab-separated-values"

            encoded = response_body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
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


def qlever_json_term(value: str) -> str:
    if value.startswith(("http://", "https://", "urn:")):
        return f"<{value}>"
    return value


def qlever_results_json(query: str, body: str) -> str:
    lines = [line for line in body.splitlines() if line]
    if not lines:
        selected: list[str] = []
        rows: list[list[str]] = []
    else:
        selected = lines[0].split("\t")
        rows = [[qlever_json_term(value) for value in line.split("\t")] for line in lines[1:]]

    return json.dumps(
        {
            "query": query,
            "selected": selected,
            "status": "OK",
            "warnings": [],
            "res": rows,
            "resultSizeExported": len(rows),
            "resultSizeTotal": len(rows),
            "resultsize": len(rows),
            "runtimeInformation": {
                "meta": {
                    "time_query_planning": 7,
                },
                "query_execution_tree": {},
                "time": {
                    "computeResult": "11ms",
                    "total": "13ms",
                },
            },
        }
    )


def query_resource_values(query: str) -> set[str]:
    prefix = "VALUES ?resource {"
    start = query.index(prefix) + len(prefix)
    end = query.index("}", start)
    return {
        term[1:-1]
        for term in query[start:end].split()
        if term.startswith("<") and term.endswith(">")
    }


def query_predicate_values(query: str) -> set[str] | None:
    prefix = "VALUES ?predicate {"
    if prefix not in query:
        return None
    start = query.index(prefix) + len(prefix)
    end = query.index("}", start)
    return {
        term[1:-1]
        for term in query[start:end].split()
        if term.startswith("<") and term.endswith(">")
    }


def graph_predicates_tsv() -> str:
    return "\n".join(
        [
            "?predicate",
            "https://w3id.org/biolink/vocab/treats",
            "https://w3id.org/biolink/vocab/affects",
            "https://w3id.org/biolink/vocab/related_to",
            "https://w3id.org/biolink/vocab/causes",
            "https://w3id.org/biolink/vocab/genetically_associated_with",
            "https://w3id.org/biolink/vocab/has_phenotype",
            "https://w3id.org/biolink/vocab/subclass_of",
            "",
        ]
    )


def single_edge_result_tsv() -> str:
    return "\n".join(
        [
            "?node_0_n0\t?node_1_n1\t?edge_0_e0\t?predicate_0_e0\t?orientation_0_e0",
            "https://identifiers.org/CHEBI:45783\thttps://identifiers.org/MONDO:0004979\turn:uuid:test-edge\thttps://w3id.org/biolink/vocab/treats\tforward",
            "",
        ]
    )


def chain_result_tsv() -> str:
    return "\n".join(
        [
            "?node_0_n0\t?node_1_n1\t?node_2_n2\t?edge_0_e0\t?edge_1_e1\t?predicate_0_e0\t?predicate_1_e1\t?orientation_0_e0\t?orientation_1_e1",
            "https://identifiers.org/CHEBI:45783\thttps://identifiers.org/NCBIGene:1017\thttps://identifiers.org/MONDO:0004979\turn:uuid:edge-affects\turn:uuid:edge-related\thttps://w3id.org/biolink/vocab/affects\thttps://w3id.org/biolink/vocab/related_to\tforward\tforward",
            "",
        ]
    )


def branch_result_tsv() -> str:
    return "\n".join(
        [
            "?node_0_gene\t?node_1_disease\t?node_2_phenotype\t?edge_0_e0\t?edge_1_e1\t?predicate_0_e0\t?predicate_1_e1\t?orientation_0_e0\t?orientation_1_e1",
            "https://identifiers.org/NCBIGene:1017\thttps://identifiers.org/MONDO:0004979\thttps://identifiers.org/HP:0001627\turn:uuid:edge-gene-disease\turn:uuid:edge-gene-phenotype\thttps://w3id.org/biolink/vocab/related_to\thttps://w3id.org/biolink/vocab/causes\tforward\tforward",
            "",
        ]
    )


def inverse_result_tsv() -> str:
    return "\n".join(
        [
            "?node_0_n0\t?node_1_n1\t?edge_0_e0\t?predicate_0_e0\t?orientation_0_e0",
            "https://identifiers.org/HP:0012592\thttps://identifiers.org/MONDO:0005148\turn:uuid:t2d-has-phenotype\thttps://w3id.org/biolink/vocab/has_phenotype\treverse",
            "",
        ]
    )


def symmetric_result_tsv() -> str:
    return "\n".join(
        [
            "?node_0_n0\t?node_1_n1\t?edge_0_e0\t?predicate_0_e0\t?orientation_0_e0",
            "https://identifiers.org/NCBIGene:1017\thttps://identifiers.org/MONDO:0005148\turn:uuid:t2d-ga-cdk2\thttps://w3id.org/biolink/vocab/genetically_associated_with\treverse",
            "",
        ]
    )


def node_constraint_result_tsv() -> str:
    return "\n".join(
        [
            "?node_0_n0",
            "https://identifiers.org/NCBIGene:1017",
            "",
        ]
    )


def all_nodes_result_tsv() -> str:
    return "\n".join(
        [
            "?node_0_n0",
            "https://identifiers.org/MONDO:0004979",
            "https://identifiers.org/MONDO:0005148",
            "",
        ]
    )


def node_only_root_result_tsv() -> str:
    return "\n".join(
        [
            "?node_0_n0",
            "https://identifiers.org/MONDO:0000001",
            "",
        ]
    )


def numeric_constraint_result_tsv() -> str:
    return "\n".join(
        [
            "?node_0_n0",
            "https://identifiers.org/NCBIGene:836",
            "",
        ]
    )


def empty_edge_result_tsv() -> str:
    return "\n".join(
        [
            "?node_0_n0\t?node_1_n1\t?edge_0_e0\t?predicate_0_e0\t?orientation_0_e0",
            "",
        ]
    )


def meta_knowledge_graph_edge_tsv() -> str:
    return "\n".join(
        [
            "?subject_category\t?predicate\t?object_category",
            "https://w3id.org/biolink/vocab/ChemicalEntity\thttps://w3id.org/biolink/vocab/treats\thttps://w3id.org/biolink/vocab/Disease",
            "https://w3id.org/biolink/vocab/ChemicalEntity\thttps://w3id.org/biolink/vocab/affects\thttps://w3id.org/biolink/vocab/Gene",
            "https://w3id.org/biolink/vocab/Gene\thttps://w3id.org/biolink/vocab/related_to\thttps://w3id.org/biolink/vocab/Gene",
            "https://w3id.org/biolink/vocab/Gene\thttps://w3id.org/biolink/vocab/related_to\thttps://w3id.org/biolink/vocab/Disease",
            "https://w3id.org/biolink/vocab/Gene\thttps://w3id.org/biolink/vocab/causes\thttps://w3id.org/biolink/vocab/PhenotypicFeature",
            "https://w3id.org/biolink/vocab/Gene\thttps://w3id.org/biolink/vocab/gene_associated_with_condition\thttps://w3id.org/biolink/vocab/Disease",
            "https://w3id.org/biolink/vocab/Disease\thttps://w3id.org/biolink/vocab/subclass_of\thttps://w3id.org/biolink/vocab/Disease",
            "https://w3id.org/biolink/vocab/Disease\thttps://w3id.org/biolink/vocab/has_phenotype\thttps://w3id.org/biolink/vocab/PhenotypicFeature",
            "https://w3id.org/biolink/vocab/Disease\thttps://w3id.org/biolink/vocab/genetically_associated_with\thttps://w3id.org/biolink/vocab/Gene",
            "https://w3id.org/biolink/vocab/Disease\thttps://w3id.org/biolink/vocab/related_to\thttps://w3id.org/biolink/vocab/Gene",
            "https://w3id.org/biolink/vocab/ChemicalEntity\thttps://w3id.org/biolink/vocab/causes\thttps://w3id.org/biolink/vocab/Disease",
            "",
        ]
    )


def meta_knowledge_graph_node_tsv() -> str:
    return "\n".join(
        [
            "?category\t?node",
            "https://w3id.org/biolink/vocab/ChemicalEntity\thttps://identifiers.org/CHEBI:45783",
            "https://w3id.org/biolink/vocab/ChemicalEntity\thttps://identifiers.org/PUBCHEM.COMPOUND:5460341",
            "https://w3id.org/biolink/vocab/ChemicalEntity\thttps://identifiers.org/MESH:D014612",
            "https://w3id.org/biolink/vocab/Disease\thttps://identifiers.org/MONDO:0004979",
            "https://w3id.org/biolink/vocab/Gene\thttps://identifiers.org/NCBIGene:1017",
            "https://w3id.org/biolink/vocab/PhenotypicFeature\thttps://identifiers.org/HP:0000118",
            "",
        ]
    )


def properties_tsv(
    resources: set[str] | None = None,
    predicates: set[str] | None = None,
) -> str:
    rows = [
        "?resource\t?predicate\t?value",
        f"https://identifiers.org/CHEBI:45783\t{RDFS_LABEL}\t\"Imatinib\"",
        "https://identifiers.org/CHEBI:45783\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#type\thttps://w3id.org/biolink/vocab/ChemicalEntity",
        f"https://identifiers.org/NCBIGene:1017\t{RDFS_LABEL}\t\"CDK2\"",
        "https://identifiers.org/NCBIGene:1017\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#type\thttps://w3id.org/biolink/vocab/Gene",
        f"https://identifiers.org/NCBIGene:1017\t{KGX_SLOT_NS}chromosome\t\"17\"",
        f"https://identifiers.org/MONDO:0004979\t{RDFS_LABEL}\t\"asthma\"",
        "https://identifiers.org/MONDO:0004979\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#type\thttps://w3id.org/biolink/vocab/Disease",
        f"https://identifiers.org/HP:0001627\t{RDFS_LABEL}\t\"Abnormal heart morphology\"",
        "https://identifiers.org/HP:0001627\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#type\thttps://w3id.org/biolink/vocab/PhenotypicFeature",
        f"https://identifiers.org/MONDO:0000001\t{RDFS_LABEL}\t\"disease\"",
        "https://identifiers.org/MONDO:0000001\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#type\thttps://w3id.org/biolink/vocab/Disease",
        f"https://identifiers.org/MONDO:0005148\t{RDFS_LABEL}\t\"type 2 diabetes\"",
        "https://identifiers.org/MONDO:0005148\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#type\thttps://w3id.org/biolink/vocab/Disease",
        f"https://identifiers.org/HP:0012592\t{RDFS_LABEL}\t\"Albuminuria\"",
        "https://identifiers.org/HP:0012592\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#type\thttps://w3id.org/biolink/vocab/PhenotypicFeature",
        f"https://identifiers.org/NCBIGene:283871\t{RDFS_LABEL}\t\"GENE283871\"",
        "https://identifiers.org/NCBIGene:283871\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#type\thttps://w3id.org/biolink/vocab/Gene",
        f"https://identifiers.org/PUBCHEM.COMPOUND:5460341\t{RDFS_LABEL}\t\"Compound5460341\"",
        "https://identifiers.org/PUBCHEM.COMPOUND:5460341\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#type\thttps://w3id.org/biolink/vocab/ChemicalEntity",
        "urn:uuid:test-edge\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#subject\thttps://identifiers.org/CHEBI:45783",
        "urn:uuid:test-edge\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#predicate\thttps://w3id.org/biolink/vocab/treats",
        "urn:uuid:test-edge\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#object\thttps://identifiers.org/MONDO:0004979",
        f"urn:uuid:test-edge\t{KGX_SLOT_NS}sources\turn:uuid:test-edge-source-primary",
        f"urn:uuid:test-edge\t{KGX_SLOT_NS}sources\turn:uuid:test-edge-source-aggregator",
        "urn:uuid:test-edge\thttps://w3id.org/kgx/slot/publications\thttps://identifiers.org/PMID:123",
        "urn:uuid:test-edge\thttps://w3id.org/kgx/slot/fda_approved\t\"true\"^^<http://www.w3.org/2001/XMLSchema#boolean>",
        "urn:uuid:test-edge\thttps://w3id.org/biolink/vocab/qualified_predicate\thttps://w3id.org/biolink/vocab/causes",
        "urn:uuid:test-edge-source-primary\thttps://w3id.org/biolink/vocab/resource_id\thttps://identifiers.org/infores:test-kp",
        "urn:uuid:test-edge-source-primary\thttps://w3id.org/biolink/vocab/resource_role\thttps://w3id.org/biolink/enum/ResourceRoleEnum/primary_knowledge_source",
        "urn:uuid:test-edge-source-aggregator\thttps://w3id.org/biolink/vocab/resource_id\thttps://identifiers.org/infores:test-ara",
        "urn:uuid:test-edge-source-aggregator\thttps://w3id.org/biolink/vocab/resource_role\thttps://w3id.org/biolink/enum/ResourceRoleEnum/aggregator_knowledge_source",
        "urn:uuid:test-edge-source-aggregator\thttps://w3id.org/biolink/vocab/upstream_resource_ids\thttps://identifiers.org/infores:test-kp",
        "urn:uuid:edge-affects\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#subject\thttps://identifiers.org/CHEBI:45783",
        "urn:uuid:edge-affects\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#predicate\thttps://w3id.org/biolink/vocab/affects",
        "urn:uuid:edge-affects\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#object\thttps://identifiers.org/NCBIGene:1017",
        "urn:uuid:edge-affects\thttps://w3id.org/biolink/vocab/primary_knowledge_source\tinfores:test-kp",
        "urn:uuid:edge-related\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#subject\thttps://identifiers.org/NCBIGene:1017",
        "urn:uuid:edge-related\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#predicate\thttps://w3id.org/biolink/vocab/related_to",
        "urn:uuid:edge-related\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#object\thttps://identifiers.org/MONDO:0004979",
        "urn:uuid:edge-related\thttps://w3id.org/biolink/vocab/primary_knowledge_source\tinfores:test-kp",
        "urn:uuid:edge-related\thttps://w3id.org/biolink/vocab/aggregator_knowledge_source\tinfores:test-ara",
        "urn:uuid:edge-gene-disease\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#subject\thttps://identifiers.org/NCBIGene:1017",
        "urn:uuid:edge-gene-disease\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#predicate\thttps://w3id.org/biolink/vocab/related_to",
        "urn:uuid:edge-gene-disease\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#object\thttps://identifiers.org/MONDO:0004979",
        "urn:uuid:edge-gene-disease\thttps://w3id.org/biolink/vocab/primary_knowledge_source\tinfores:test-kp",
        "urn:uuid:edge-gene-phenotype\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#subject\thttps://identifiers.org/NCBIGene:1017",
        "urn:uuid:edge-gene-phenotype\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#predicate\thttps://w3id.org/biolink/vocab/causes",
        "urn:uuid:edge-gene-phenotype\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#object\thttps://identifiers.org/HP:0001627",
        "urn:uuid:edge-gene-phenotype\thttps://w3id.org/biolink/vocab/primary_knowledge_source\tinfores:test-kp",
        "urn:uuid:edge-gene-phenotype\thttps://w3id.org/biolink/vocab/supporting_data_source\tinfores:test-source",
        "urn:uuid:t2d-has-phenotype\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#subject\thttps://identifiers.org/MONDO:0005148",
        "urn:uuid:t2d-has-phenotype\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#predicate\thttps://w3id.org/biolink/vocab/has_phenotype",
        "urn:uuid:t2d-has-phenotype\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#object\thttps://identifiers.org/HP:0012592",
        "urn:uuid:t2d-has-phenotype\thttps://w3id.org/biolink/vocab/primary_knowledge_source\tinfores:test-kp",
        "urn:uuid:t2d-isa-disease\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#subject\thttps://identifiers.org/MONDO:0005148",
        "urn:uuid:t2d-isa-disease\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#predicate\thttps://w3id.org/biolink/vocab/subclass_of",
        "urn:uuid:t2d-isa-disease\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#object\thttps://identifiers.org/MONDO:0000001",
        "urn:uuid:t2d-isa-disease\thttps://w3id.org/biolink/vocab/primary_knowledge_source\tinfores:test-kp",
        "urn:uuid:qualified-edge\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#subject\thttps://identifiers.org/PUBCHEM.COMPOUND:5460341",
        "urn:uuid:qualified-edge\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#predicate\thttps://w3id.org/biolink/vocab/affects",
        "urn:uuid:qualified-edge\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#object\thttps://identifiers.org/NCBIGene:283871",
        "urn:uuid:qualified-edge\thttps://w3id.org/biolink/vocab/primary_knowledge_source\tinfores:test-kp",
        "urn:uuid:qualified-edge\thttps://w3id.org/biolink/vocab/qualified_predicate\thttps://w3id.org/biolink/vocab/causes",
        "urn:uuid:qualified-edge\thttps://w3id.org/biolink/vocab/object_aspect_qualifier\thttps://w3id.org/biolink/enum/GeneOrGeneProductOrChemicalEntityAspectEnum/activity",
        "urn:uuid:qualified-edge\thttps://w3id.org/biolink/vocab/object_direction_qualifier\thttps://w3id.org/biolink/enum/DirectionQualifierEnum/decreased",
        "urn:uuid:t2d-ga-cdk2\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#subject\thttps://identifiers.org/MONDO:0005148",
        "urn:uuid:t2d-ga-cdk2\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#predicate\thttps://w3id.org/biolink/vocab/genetically_associated_with",
        "urn:uuid:t2d-ga-cdk2\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#object\thttps://identifiers.org/NCBIGene:1017",
        "urn:uuid:t2d-ga-cdk2\thttps://w3id.org/biolink/vocab/primary_knowledge_source\tinfores:test-kp",
        f"https://identifiers.org/NCBIGene:836\t{RDFS_LABEL}\t\"CASP3\"",
        "https://identifiers.org/NCBIGene:836\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#type\thttps://w3id.org/biolink/vocab/Gene",
        f"https://identifiers.org/NCBIGene:836\t{KGX_SLOT_NS}length\t\"277\"^^<http://www.w3.org/2001/XMLSchema#integer>",
        f"https://identifiers.org/NCBIGene:841\t{RDFS_LABEL}\t\"GENE841\"",
        "https://identifiers.org/NCBIGene:841\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#type\thttps://w3id.org/biolink/vocab/Gene",
        f"https://identifiers.org/NCBIGene:672\t{RDFS_LABEL}\t\"BRCA1\"",
        "https://identifiers.org/NCBIGene:672\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#type\thttps://w3id.org/biolink/vocab/Gene",
        f"https://identifiers.org/MONDO:0004993\t{RDFS_LABEL}\t\"disease 4993\"",
        "https://identifiers.org/MONDO:0004993\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#type\thttps://w3id.org/biolink/vocab/Disease",
        f"https://identifiers.org/MESH:D014612\t{RDFS_LABEL}\t\"MESH D014612\"",
        "https://identifiers.org/MESH:D014612\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#type\thttps://w3id.org/biolink/vocab/ChemicalEntity",
        f"https://identifiers.org/MONDO:0005260\t{RDFS_LABEL}\t\"disease 5260\"",
        "https://identifiers.org/MONDO:0005260\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#type\thttps://w3id.org/biolink/vocab/Disease",
        f"https://identifiers.org/HP:0000118\t{RDFS_LABEL}\t\"Phenotypic abnormality\"",
        "https://identifiers.org/HP:0000118\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#type\thttps://w3id.org/biolink/vocab/PhenotypicFeature",
        "urn:uuid:edge-publications\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#subject\thttps://identifiers.org/NCBIGene:836",
        "urn:uuid:edge-publications\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#predicate\thttps://w3id.org/biolink/vocab/related_to",
        "urn:uuid:edge-publications\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#object\thttps://identifiers.org/NCBIGene:841",
        "urn:uuid:edge-publications\thttps://w3id.org/biolink/vocab/primary_knowledge_source\tinfores:test-kp",
        "urn:uuid:edge-publications\thttps://w3id.org/kgx/slot/publications\thttps://identifiers.org/PMID:123",
        "urn:uuid:edge-p-value\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#subject\thttps://identifiers.org/MONDO:0005148",
        "urn:uuid:edge-p-value\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#predicate\thttps://w3id.org/biolink/vocab/related_to",
        "urn:uuid:edge-p-value\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#object\thttps://identifiers.org/NCBIGene:841",
        "urn:uuid:edge-p-value\thttps://w3id.org/biolink/vocab/primary_knowledge_source\tinfores:test-kp",
        "urn:uuid:edge-p-value\thttps://w3id.org/kgx/slot/p_value\t\"0.000007\"^^<http://www.w3.org/2001/XMLSchema#double>",
        "urn:uuid:edge-non-biolink-attribute\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#subject\thttps://identifiers.org/NCBIGene:836",
        "urn:uuid:edge-non-biolink-attribute\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#predicate\thttps://w3id.org/biolink/vocab/related_to",
        "urn:uuid:edge-non-biolink-attribute\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#object\thttps://identifiers.org/MONDO:0005148",
        "urn:uuid:edge-non-biolink-attribute\thttps://w3id.org/biolink/vocab/primary_knowledge_source\tinfores:test-kp",
        "urn:uuid:edge-non-biolink-attribute\thttps://w3id.org/kgx/slot/non_biolink_attribute\t\"xxx123\"",
        "urn:uuid:edge-json-attributes\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#subject\thttps://identifiers.org/NCBIGene:672",
        "urn:uuid:edge-json-attributes\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#predicate\thttps://w3id.org/biolink/vocab/gene_associated_with_condition",
        "urn:uuid:edge-json-attributes\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#object\thttps://identifiers.org/MONDO:0004993",
        "urn:uuid:edge-json-attributes\thttps://w3id.org/biolink/vocab/primary_knowledge_source\tinfores:test-kp",
        "urn:uuid:edge-json-attributes\thttps://w3id.org/kgx/slot/publications\thttps://identifiers.org/PMID:123",
        'urn:uuid:edge-json-attributes\thttps://w3id.org/kgx/slot/attributes\t{"attribute_type_id": "json_attribute_1", "value": "json_value_1"}',
        f"urn:uuid:edge-json-attributes\t{KGX_SLOT_NS}attributes\turn:uuid:json-attribute-2",
        f"urn:uuid:edge-json-attributes\t{KGX_SLOT_NS}attributes\turn:uuid:json-attribute-3",
        f"urn:uuid:json-attribute-2\t{KGX_SLOT_NS}attribute_type_id\t\"json_attribute_2\"",
        f"urn:uuid:json-attribute-3\t{KGX_SLOT_NS}attribute_type_id\t\"json_attribute_3\"",
        f"urn:uuid:json-attribute-3\t{KGX_SLOT_NS}value\t\"json_value_3\"",
        f"urn:uuid:json-attribute-3\t{KGX_SLOT_NS}attributes\turn:uuid:nested-json-attribute-1",
        f"urn:uuid:nested-json-attribute-1\t{KGX_SLOT_NS}attribute_type_id\t\"nested_json_attribute_1\"",
        f"urn:uuid:nested-json-attribute-1\t{KGX_SLOT_NS}value\t\"nested_json_value_1\"",
        "urn:uuid:edge-missing-primary\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#subject\thttps://identifiers.org/MESH:D014612",
        "urn:uuid:edge-missing-primary\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#predicate\thttps://w3id.org/biolink/vocab/causes",
        "urn:uuid:edge-missing-primary\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#object\thttps://identifiers.org/MONDO:0005260",
        "urn:uuid:qualified-edge-aspect-only\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#subject\thttps://identifiers.org/PUBCHEM.COMPOUND:5460341",
        "urn:uuid:qualified-edge-aspect-only\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#predicate\thttps://w3id.org/biolink/vocab/affects",
        "urn:uuid:qualified-edge-aspect-only\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#object\thttps://identifiers.org/NCBIGene:283871",
        "urn:uuid:qualified-edge-aspect-only\thttps://w3id.org/biolink/vocab/primary_knowledge_source\tinfores:test-kp",
        "urn:uuid:qualified-edge-aspect-only\thttps://w3id.org/biolink/vocab/object_aspect_qualifier\thttps://w3id.org/biolink/enum/GeneOrGeneProductOrChemicalEntityAspectEnum/activity",
        "urn:uuid:albuminuria-subclass-phenotype\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#subject\thttps://identifiers.org/HP:0012592",
        "urn:uuid:albuminuria-subclass-phenotype\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#predicate\thttps://w3id.org/biolink/vocab/subclass_of",
        "urn:uuid:albuminuria-subclass-phenotype\thttp://www.w3.org/1999/02/22-rdf-syntax-ns#object\thttps://identifiers.org/HP:0000118",
        "urn:uuid:albuminuria-subclass-phenotype\thttps://w3id.org/biolink/vocab/primary_knowledge_source\tinfores:test-kp",
    ]
    if resources is not None:
        rows = [rows[0]] + [row for row in rows[1:] if row.split("\t", 1)[0] in resources]
    if predicates is not None:
        rows = [rows[0]] + [row for row in rows[1:] if row.split("\t")[1] in predicates]
    return "\n".join(rows) + "\n"


def subclass_result_tsv() -> str:
    return "\n".join(
        [
            "?node_0_n0\t?node_1_n1\t?node_2_n0_superclass\t?edge_0_e0\t?edge_1_n0_subclass_edge\t?predicate_0_e0\t?orientation_0_e0",
            "https://identifiers.org/MONDO:0005148\thttps://identifiers.org/HP:0012592\thttps://identifiers.org/MONDO:0000001\turn:uuid:t2d-has-phenotype\turn:uuid:t2d-isa-disease\thttps://w3id.org/biolink/vocab/has_phenotype\tforward",
            "",
        ]
    )


def qualified_result_tsv() -> str:
    return "\n".join(
        [
            "?node_0_n0\t?node_1_n1\t?edge_0_e0\t?predicate_0_e0\t?orientation_0_e0",
            "https://identifiers.org/PUBCHEM.COMPOUND:5460341\thttps://identifiers.org/NCBIGene:283871\turn:uuid:qualified-edge\thttps://w3id.org/biolink/vocab/affects\tforward",
            "",
        ]
    )


def multi_qualified_result_tsv() -> str:
    return "\n".join(
        [
            "?node_0_n0\t?node_1_n1\t?edge_0_e0\t?predicate_0_e0\t?orientation_0_e0",
            "https://identifiers.org/PUBCHEM.COMPOUND:5460341\thttps://identifiers.org/NCBIGene:283871\turn:uuid:qualified-edge\thttps://w3id.org/biolink/vocab/affects\tforward",
            "https://identifiers.org/PUBCHEM.COMPOUND:5460341\thttps://identifiers.org/NCBIGene:283871\turn:uuid:qualified-edge-aspect-only\thttps://w3id.org/biolink/vocab/affects\tforward",
            "",
        ]
    )


def publications_edge_result_tsv() -> str:
    return "\n".join(
        [
            "?node_0_n0\t?node_1_n1\t?edge_0_e0\t?predicate_0_e0\t?orientation_0_e0",
            "https://identifiers.org/NCBIGene:836\thttps://identifiers.org/NCBIGene:841\turn:uuid:edge-publications\thttps://w3id.org/biolink/vocab/related_to\tforward",
            "",
        ]
    )


def p_value_result_tsv() -> str:
    return "\n".join(
        [
            "?node_0_n0\t?node_1_n1\t?edge_0_e0\t?predicate_0_e0\t?orientation_0_e0",
            "https://identifiers.org/MONDO:0005148\thttps://identifiers.org/NCBIGene:841\turn:uuid:edge-p-value\thttps://w3id.org/biolink/vocab/related_to\tforward",
            "",
        ]
    )


def generic_attribute_result_tsv() -> str:
    return "\n".join(
        [
            "?node_0_n0\t?node_1_n1\t?edge_0_e0\t?predicate_0_e0\t?orientation_0_e0",
            "https://identifiers.org/NCBIGene:836\thttps://identifiers.org/MONDO:0005148\turn:uuid:edge-non-biolink-attribute\thttps://w3id.org/biolink/vocab/related_to\tforward",
            "",
        ]
    )


def json_attribute_result_tsv() -> str:
    return "\n".join(
        [
            "?node_0_n0\t?node_1_n1\t?edge_0_e0\t?predicate_0_e0\t?orientation_0_e0",
            "https://identifiers.org/NCBIGene:672\thttps://identifiers.org/MONDO:0004993\turn:uuid:edge-json-attributes\thttps://w3id.org/biolink/vocab/gene_associated_with_condition\tforward",
            "",
        ]
    )


def missing_primary_source_result_tsv() -> str:
    return "\n".join(
        [
            "?node_0_n0\t?node_1_n1\t?edge_0_e0\t?predicate_0_e0\t?orientation_0_e0",
            "https://identifiers.org/MESH:D014612\thttps://identifiers.org/MONDO:0005260\turn:uuid:edge-missing-primary\thttps://w3id.org/biolink/vocab/causes\tforward",
            "",
        ]
    )


def inverse_subclass_result_tsv() -> str:
    return "\n".join(
        [
            "?node_0_n0\t?node_1_n1\t?node_2_n0_superclass\t?edge_0_e0\t?edge_1_n0_subclass_edge\t?predicate_0_e0\t?orientation_0_e0",
            "https://identifiers.org/HP:0012592\thttps://identifiers.org/MONDO:0005148\thttps://identifiers.org/HP:0000118\turn:uuid:t2d-has-phenotype\turn:uuid:albuminuria-subclass-phenotype\thttps://w3id.org/biolink/vocab/has_phenotype\treverse",
            "",
        ]
    )


def test_answer_trapi_request_returns_metadata_rich_edge_payload(
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

    edge = response["message"]["knowledge_graph"]["edges"]["urn:uuid:test-edge"]
    assert response["message"]["knowledge_graph"]["nodes"]["CHEBI:45783"] == {
        "categories": ["biolink:ChemicalEntity"],
        "name": "Imatinib",
    }
    assert response["message"]["knowledge_graph"]["nodes"]["MONDO:0004979"] == {
        "categories": ["biolink:Disease"],
        "name": "asthma",
    }
    assert edge["subject"] == "CHEBI:45783"
    assert edge["predicate"] == "biolink:treats"
    assert edge["object"] == "MONDO:0004979"
    assert edge["sources"] == [
        {
            "resource_id": "infores:test-kp",
            "resource_role": "primary_knowledge_source",
        },
        {
            "resource_id": "infores:test-ara",
            "resource_role": "aggregator_knowledge_source",
            "upstream_resource_ids": ["infores:test-kp"],
        },
        {
            "resource_id": "infores:qlever-trapi-test",
            "resource_role": "aggregator_knowledge_source",
            "upstream_resource_ids": ["infores:test-ara"],
        },
    ]
    assert edge["qualifiers"] == [
        {
            "qualifier_type_id": "biolink:qualified_predicate",
            "qualifier_value": "biolink:causes",
        }
    ]
    assert {
        "original_attribute_name": "publications",
        "attribute_type_id": "biolink:publications",
        "value": ["PMID:123"],
        "value_type_id": "linkml:Uriorcurie",
    } in edge["attributes"]
    assert {
        "original_attribute_name": "fda_approved",
        "attribute_type_id": "biolink:Attribute",
        "value": True,
    } in edge["attributes"]
    assert response["message"]["results"] == [
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
    ]


def test_answer_trapi_request_includes_granular_timing(
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

    assert response["timing"]["total_ms"] >= 0
    assert response["timing"]["trapi_to_sparql"]["normalize_request_ms"] >= 0
    assert response["timing"]["trapi_to_sparql"]["build_sparql_ms"] >= 0
    assert response["timing"]["primary_query"]["row_count"] == 1
    assert response["timing"]["primary_query"]["qlever"] == {
        "planning_ms": 7,
        "compute_result_ms": 11,
        "total_ms": 13,
    }
    assert response["timing"]["property_fetch"]["resource_count"] == 3
    assert response["timing"]["property_fetch"]["node_resource_count"] == 2
    assert response["timing"]["property_fetch"]["edge_resource_count"] == 1
    assert response["timing"]["property_fetch"]["initial_query"]["qlever"]["total_ms"] == 13
    assert response["timing"]["property_fetch"]["linked_expansion"]["iteration_count"] == 1
    assert response["timing"]["property_fetch"]["linked_expansion"]["iterations"][0]["resource_count"] == 2
    assert response["timing"]["trapi_response"]["build_knowledge_graph_ms"] >= 0
    assert response["timing"]["trapi_response"]["build_results_ms"] >= 0
    assert response["timing"]["counts"] == {
        "query_row_count": 1,
        "node_count": 2,
        "edge_count": 1,
        "result_count": 1,
        "auxiliary_graph_count": 0,
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

    assert response["message"]["knowledge_graph"]["nodes"]["NCBIGene:1017"]["name"] == "CDK2"
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


def test_answer_trapi_request_supports_inverse_predicates(
    qlever_test_server: tuple[str, int],
) -> None:
    host, port = qlever_test_server

    response = answer_trapi_request(
        inverse_predicate_request(),
        host_name=host,
        port=port,
        limit=10,
        resource_id="infores:qlever-trapi-test",
        subclass_depth=0,
    )

    result = response["message"]["results"][0]
    assert result["node_bindings"] == {
        "n0": [{"id": "HP:0012592"}],
        "n1": [{"id": "MONDO:0005148"}],
    }
    assert result["analyses"][0]["edge_bindings"] == {
        "e0": [{"id": "urn:uuid:t2d-has-phenotype"}],
    }
    edge = response["message"]["knowledge_graph"]["edges"]["urn:uuid:t2d-has-phenotype"]
    assert edge["subject"] == "MONDO:0005148"
    assert edge["predicate"] == "biolink:has_phenotype"
    assert edge["object"] == "HP:0012592"


def test_answer_trapi_request_supports_symmetric_predicates(
    qlever_test_server: tuple[str, int],
) -> None:
    host, port = qlever_test_server

    response = answer_trapi_request(
        symmetric_predicate_request(),
        host_name=host,
        port=port,
        limit=10,
        resource_id="infores:qlever-trapi-test",
        subclass_depth=0,
    )

    result = response["message"]["results"][0]
    assert result["node_bindings"] == {
        "n0": [{"id": "NCBIGene:1017"}],
        "n1": [{"id": "MONDO:0005148"}],
    }
    assert result["analyses"][0]["edge_bindings"] == {
        "e0": [{"id": "urn:uuid:t2d-ga-cdk2"}],
    }


def test_answer_trapi_request_supports_qnode_constraints(
    qlever_test_server: tuple[str, int],
) -> None:
    host, port = qlever_test_server

    response = answer_trapi_request(
        node_constraint_request(),
        host_name=host,
        port=port,
        limit=10,
        resource_id="infores:qlever-trapi-test",
        subclass_depth=1,
    )

    assert response["message"]["knowledge_graph"]["edges"] == {}
    assert response["message"]["results"] == [
        {
            "node_bindings": {
                "n0": [{"id": "NCBIGene:1017"}],
            },
            "analyses": [
                {
                    "resource_id": "infores:qlever-trapi-test",
                    "edge_bindings": {},
                }
            ],
        }
    ]


def test_answer_trapi_request_supports_qedge_attribute_constraints(
    qlever_test_server: tuple[str, int],
) -> None:
    host, port = qlever_test_server

    response = answer_trapi_request(
        edge_constraint_request(),
        host_name=host,
        port=port,
        limit=10,
        resource_id="infores:qlever-trapi-test",
        subclass_depth=0,
    )

    assert response["message"]["results"][0]["analyses"][0]["edge_bindings"] == {
        "e0": [{"id": "urn:uuid:test-edge"}],
    }


def test_answer_trapi_request_supports_set_interpretation_all(
    qlever_test_server: tuple[str, int],
) -> None:
    host, port = qlever_test_server

    response = answer_trapi_request(
        all_nodes_request(),
        host_name=host,
        port=port,
        limit=10,
        resource_id="infores:qlever-trapi-test",
        subclass_depth=1,
    )

    assert response["message"]["knowledge_graph"]["edges"] == {}
    assert response["message"]["results"] == [
        {
            "node_bindings": {
                "n0": [
                    {"id": "MONDO:0004979"},
                    {"id": "MONDO:0005148"},
                ],
            },
            "analyses": [
                {
                    "resource_id": "infores:qlever-trapi-test",
                    "edge_bindings": {},
                }
            ],
        }
    ]


def test_answer_trapi_request_supports_any_predicate(
    qlever_test_server: tuple[str, int],
) -> None:
    host, port = qlever_test_server

    response = answer_trapi_request(
        no_predicate_request(),
        host_name=host,
        port=port,
        limit=10,
        resource_id="infores:qlever-trapi-test",
        subclass_depth=0,
    )

    assert response["message"]["results"][0]["analyses"][0]["edge_bindings"] == {
        "e0": [{"id": "urn:uuid:test-edge"}],
    }
    assert response["message"]["knowledge_graph"]["edges"]["urn:uuid:test-edge"]["predicate"] == "biolink:treats"


def test_answer_trapi_request_supports_root_predicate(
    qlever_test_server: tuple[str, int],
) -> None:
    host, port = qlever_test_server

    response = answer_trapi_request(
        related_to_request(),
        host_name=host,
        port=port,
        limit=10,
        resource_id="infores:qlever-trapi-test",
        subclass_depth=0,
    )

    assert response["message"]["results"][0]["analyses"][0]["edge_bindings"] == {
        "e0": [{"id": "urn:uuid:test-edge"}],
    }
    assert response["message"]["knowledge_graph"]["edges"]["urn:uuid:test-edge"]["predicate"] == "biolink:treats"


def test_answer_trapi_request_supports_numeric_qnode_constraints(
    qlever_test_server: tuple[str, int],
) -> None:
    host, port = qlever_test_server

    response = answer_trapi_request(
        numeric_constraint_request(),
        host_name=host,
        port=port,
        limit=10,
        resource_id="infores:qlever-trapi-test",
        subclass_depth=0,
    )

    node = response["message"]["knowledge_graph"]["nodes"]["NCBIGene:836"]
    assert node["name"] == "CASP3"
    assert "length" not in node
    assert response["message"]["results"] == [
        {
            "node_bindings": {
                "n0": [{"id": "NCBIGene:836"}],
            },
            "analyses": [
                {
                    "resource_id": "infores:qlever-trapi-test",
                    "edge_bindings": {},
                }
            ],
        }
    ]


def test_answer_trapi_request_returns_publications_attribute(
    qlever_test_server: tuple[str, int],
) -> None:
    host, port = qlever_test_server

    response = answer_trapi_request(
        publications_request(),
        host_name=host,
        port=port,
        limit=10,
        resource_id="infores:qlever-trapi-test",
        subclass_depth=0,
    )

    edge = response["message"]["knowledge_graph"]["edges"]["urn:uuid:edge-publications"]
    assert {
        "original_attribute_name": "publications",
        "attribute_type_id": "biolink:publications",
        "value": ["PMID:123"],
        "value_type_id": "linkml:Uriorcurie",
    } in edge["attributes"]


def test_answer_trapi_request_maps_known_biolink_attributes_without_override(
    qlever_test_server: tuple[str, int],
) -> None:
    host, port = qlever_test_server

    response = answer_trapi_request(
        p_value_request(),
        host_name=host,
        port=port,
        limit=10,
        resource_id="infores:qlever-trapi-test",
        subclass_depth=0,
    )

    edge = response["message"]["knowledge_graph"]["edges"]["urn:uuid:edge-p-value"]
    assert any(
        attribute["original_attribute_name"] == "p_value"
        and attribute["attribute_type_id"] == "biolink:p_value"
        and attribute["value"] == 0.000007
        for attribute in edge["attributes"]
    )


def test_answer_trapi_request_maps_unknown_attributes_to_generic_attribute(
    qlever_test_server: tuple[str, int],
) -> None:
    host, port = qlever_test_server

    response = answer_trapi_request(
        generic_attribute_request(),
        host_name=host,
        port=port,
        limit=10,
        resource_id="infores:qlever-trapi-test",
        subclass_depth=0,
    )

    edge = response["message"]["knowledge_graph"]["edges"]["urn:uuid:edge-non-biolink-attribute"]
    assert {
        "original_attribute_name": "non_biolink_attribute",
        "attribute_type_id": "biolink:Attribute",
        "value": "xxx123",
    } in edge["attributes"]


def test_answer_trapi_request_reconstructs_nested_preformatted_attributes(
    qlever_test_server: tuple[str, int],
) -> None:
    host, port = qlever_test_server

    response = answer_trapi_request(
        json_attribute_request(),
        host_name=host,
        port=port,
        limit=10,
        resource_id="infores:qlever-trapi-test",
        subclass_depth=0,
    )

    edge = response["message"]["knowledge_graph"]["edges"]["urn:uuid:edge-json-attributes"]
    attributes = edge["attributes"]
    assert {
        "original_attribute_name": "publications",
        "attribute_type_id": "biolink:publications",
        "value": ["PMID:123"],
        "value_type_id": "linkml:Uriorcurie",
    } in attributes
    assert {"attribute_type_id": "json_attribute_1", "value": "json_value_1"} in attributes
    assert {"attribute_type_id": "json_attribute_2"} in attributes
    assert {
        "attribute_type_id": "json_attribute_3",
        "value": "json_value_3",
        "attributes": [
            {
                "attribute_type_id": "nested_json_attribute_1",
                "value": "nested_json_value_1",
            }
        ],
    } in attributes


def test_answer_trapi_request_supports_multi_qualifier_sets(
    qlever_test_server: tuple[str, int],
) -> None:
    host, port = qlever_test_server

    response = answer_trapi_request(
        multi_qualifier_request(),
        host_name=host,
        port=port,
        limit=10,
        resource_id="infores:qlever-trapi-test",
        subclass_depth=0,
    )

    edge_bindings = response["message"]["results"][0]["analyses"][0]["edge_bindings"]["e0"]
    assert {binding["id"] for binding in edge_bindings} == {
        "urn:uuid:qualified-edge",
        "urn:uuid:qualified-edge-aspect-only",
    }


def test_answer_trapi_request_returns_no_results_for_mismatched_qualifier_value(
    qlever_test_server: tuple[str, int],
) -> None:
    host, port = qlever_test_server

    response = answer_trapi_request(
        qualifier_request("abundance"),
        host_name=host,
        port=port,
        limit=10,
        resource_id="infores:qlever-trapi-test",
        subclass_depth=0,
    )

    assert response["message"]["knowledge_graph"] == {
        "nodes": {},
        "edges": {},
    }
    assert response["message"]["results"] == []


def test_answer_trapi_request_ignores_empty_qualifier_sets(
    qlever_test_server: tuple[str, int],
) -> None:
    host, port = qlever_test_server

    response = answer_trapi_request(
        empty_qualifier_request(),
        host_name=host,
        port=port,
        limit=10,
        resource_id="infores:qlever-trapi-test",
        subclass_depth=0,
    )

    assert response["message"]["results"][0]["analyses"][0]["edge_bindings"] == {
        "e0": [{"id": "urn:uuid:qualified-edge"}],
    }


def test_answer_trapi_request_falls_back_to_transpiler_source_when_primary_missing(
    qlever_test_server: tuple[str, int],
) -> None:
    host, port = qlever_test_server

    response = answer_trapi_request(
        missing_primary_source_request(),
        host_name=host,
        port=port,
        limit=10,
        resource_id="infores:qlever-trapi-test",
        subclass_depth=0,
    )

    edge = response["message"]["knowledge_graph"]["edges"]["urn:uuid:edge-missing-primary"]
    assert edge["sources"] == [
        {
            "resource_id": "infores:qlever-trapi-test",
            "resource_role": "primary_knowledge_source",
        }
    ]


def test_answer_trapi_request_keeps_node_only_queries_exact_without_subclass_reasoning(
    qlever_test_server: tuple[str, int],
) -> None:
    host, port = qlever_test_server

    response = answer_trapi_request(
        node_only_root_request(),
        host_name=host,
        port=port,
        limit=10,
        resource_id="infores:qlever-trapi-test",
        subclass_depth=1,
    )

    assert response["message"]["knowledge_graph"]["edges"] == {}
    assert response["message"]["results"] == [
        {
            "node_bindings": {
                "n0": [{"id": "MONDO:0000001"}],
            },
            "analyses": [
                {
                    "resource_id": "infores:qlever-trapi-test",
                    "edge_bindings": {},
                }
            ],
        }
    ]
    assert response["message"]["auxiliary_graphs"] == {}


def test_answer_trapi_request_preserves_inverse_subclass_orientation(
    qlever_test_server: tuple[str, int],
) -> None:
    host, port = qlever_test_server

    response = answer_trapi_request(
        inverse_subclass_request(),
        host_name=host,
        port=port,
        limit=10,
        resource_id="infores:qlever-trapi-test",
        subclass_depth=1,
    )

    result = response["message"]["results"][0]
    inferred_edge_id = result["analyses"][0]["edge_bindings"]["e0"][0]["id"]
    inferred_edge = response["message"]["knowledge_graph"]["edges"][inferred_edge_id]

    assert result["node_bindings"] == {
        "n0": [{"id": "HP:0000118"}],
        "n1": [{"id": "MONDO:0005148"}],
    }
    assert response["message"]["auxiliary_graphs"] == {
        "aux_" + inferred_edge_id.split(":", 1)[1]: {
            "attributes": [],
            "edges": [
                "urn:uuid:t2d-has-phenotype",
                "urn:uuid:albuminuria-subclass-phenotype",
            ],
        }
    }
    assert inferred_edge["subject"] == "MONDO:0005148"
    assert inferred_edge["predicate"] == "biolink:has_phenotype"
    assert inferred_edge["object"] == "HP:0000118"
    assert inferred_edge["sources"] == [
        {
            "resource_id": "infores:qlever-trapi-test",
            "resource_role": "primary_knowledge_source",
        }
    ]


def test_answer_trapi_request_returns_empty_payload_for_empty_query_graph() -> None:
    response = answer_trapi_request(empty_request(), subclass_depth=1)

    assert response["message"] == {
        "query_graph": empty_request()["message"]["query_graph"],
        "knowledge_graph": {
            "nodes": {},
            "edges": {},
        },
        "results": [],
        "auxiliary_graphs": {},
    }
    assert response["timing"]["primary_query"] is None
    assert response["timing"]["counts"] == {
        "query_row_count": 0,
        "node_count": 0,
        "edge_count": 0,
        "result_count": 0,
        "auxiliary_graph_count": 0,
    }


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


@pytest.fixture()
def fastapi_client(qlever_test_server: tuple[str, int]) -> TestClient:
    qlever_host, qlever_port = qlever_test_server
    app = create_fastapi_app(
        qlever_host_name=qlever_host,
        qlever_port=qlever_port,
        access_token=None,
        limit=50,
        resource_id="infores:qlever-trapi-fastapi-test",
        subclass_depth=0,
        async_workers=2,
        async_job_ttl_seconds=60,
        metakg_cache_seconds=0,
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def callback_capture_server() -> tuple[str, int, list[dict[str, Any]]]:
    captured: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers["Content-Length"])
            payload = self.rfile.read(length).decode("utf-8")
            captured.append(json.loads(payload))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[0], server.server_address[1], captured
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
    assert response["timing"]["primary_query"]["qlever"]["total_ms"] == 13
    assert response["message"]["knowledge_graph"]["edges"]["urn:uuid:edge-related"]["predicate"] == "biolink:related_to"
    assert response["message"]["results"][0]["analyses"][0]["resource_id"] == "infores:qlever-trapi-http-test"


def test_http_service_returns_400_for_invalid_request(trapi_service_server: tuple[str, int]) -> None:
    host, port = trapi_service_server
    bad_request = sample_request()
    bad_request["message"]["query_graph"]["nodes"]["n0"]["is_set"] = True

    status, response = http_json_request(
        "POST",
        f"http://{host}:{port}/query",
        payload=bad_request,
    )

    assert status == 400
    assert response["status"] == "BadRequest"
    assert "is_set=true" in response["description"]


def test_http_service_returns_404_for_unknown_path(trapi_service_server: tuple[str, int]) -> None:
    host, port = trapi_service_server

    status, response = http_json_request("GET", f"http://{host}:{port}/nope")

    assert status == 404
    assert response == {
        "description": "Unknown endpoint: /nope",
        "http_code": 404,
        "status": "NotFound",
    }


def test_fastapi_health_endpoint(fastapi_client: TestClient) -> None:
    response = fastapi_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "description": "TRAPI service is healthy",
        "http_code": 200,
        "status": "Success",
    }


def test_fastapi_query_endpoint_returns_trapi_envelope(fastapi_client: TestClient) -> None:
    response = fastapi_client.post("/query", json=chain_request())

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "Success"
    assert payload["description"] == "Query processed successfully"
    assert payload["http_code"] == 200
    assert payload["timing"]["primary_query"]["qlever"]["total_ms"] == 13
    assert payload["message"]["knowledge_graph"]["edges"]["urn:uuid:edge-related"]["predicate"] == "biolink:related_to"
    assert payload["message"]["results"][0]["analyses"][0]["resource_id"] == "infores:qlever-trapi-fastapi-test"


def test_fastapi_asyncquery_endpoint_completes_and_posts_callback(
    fastapi_client: TestClient,
    callback_capture_server: tuple[str, int, list[dict[str, Any]]],
) -> None:
    host, port, captured = callback_capture_server
    request = sample_request()
    request["callback"] = f"http://{host}:{port}/callback"

    response = fastapi_client.post("/asyncquery", json=request)

    assert response.status_code == 202
    submit_payload = response.json()
    assert submit_payload["status"] in {"Accepted", "Running"}
    job_id = submit_payload["job_id"]

    for _ in range(50):
        status_response = fastapi_client.get(f"/asyncquery_status/{job_id}")
        status_payload = status_response.json()
        if status_payload["status"] == "Success":
            break
        time.sleep(0.05)
    else:
        raise AssertionError(f"Async query job {job_id} did not complete in time")

    assert status_response.status_code == 200
    assert status_payload["timing"]["primary_query"]["qlever"]["total_ms"] == 13
    assert status_payload["message"]["results"] == [
        {
            "node_bindings": {
                "n0": [{"id": "CHEBI:45783"}],
                "n1": [{"id": "MONDO:0004979"}],
            },
            "analyses": [
                {
                    "resource_id": "infores:qlever-trapi-fastapi-test",
                    "edge_bindings": {
                        "e0": [{"id": "urn:uuid:test-edge"}],
                    },
                }
            ],
        }
    ]
    assert captured, "Expected asyncquery callback to be delivered"
    assert captured[0]["status"] == "Success"
    assert captured[0]["timing"] == status_payload["timing"]
    assert captured[0]["message"]["results"] == status_payload["message"]["results"]


def test_fastapi_asyncquery_status_returns_404_for_unknown_job(
    fastapi_client: TestClient,
) -> None:
    response = fastapi_client.get("/asyncquery_status/not-a-job")

    assert response.status_code == 404
    assert response.json() == {
        "description": "Unknown async query job: not-a-job",
        "http_code": 404,
        "status": "NotFound",
    }


def test_fastapi_meta_knowledge_graph_endpoint(fastapi_client: TestClient) -> None:
    response = fastapi_client.get("/meta_knowledge_graph")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "Success"
    assert payload["description"] == "Meta knowledge graph generated successfully"
    metakg = payload["meta_knowledge_graph"]
    assert metakg["nodes"]["biolink:ChemicalEntity"]["id_prefixes"] == [
        "CHEBI",
        "MESH",
        "PUBCHEM.COMPOUND",
    ]
    assert metakg["nodes"]["biolink:Disease"]["id_prefixes"] == ["MONDO"]
    assert metakg["nodes"]["biolink:Gene"]["id_prefixes"] == ["NCBIGene"]
    assert metakg["nodes"]["biolink:PhenotypicFeature"]["id_prefixes"] == ["HP"]
    assert any(
        edge == {
            "subject": "biolink:ChemicalEntity",
            "predicate": "biolink:treats",
            "object": "biolink:Disease",
        }
        for edge in metakg["edges"].values()
    )
    assert any(
        edge == {
            "subject": "biolink:Disease",
            "predicate": "biolink:has_phenotype",
            "object": "biolink:PhenotypicFeature",
        }
        for edge in metakg["edges"].values()
    )


def test_fastapi_metakg_alias_matches_meta_knowledge_graph(fastapi_client: TestClient) -> None:
    meta_knowledge_graph_response = fastapi_client.get("/meta_knowledge_graph")
    metakg_response = fastapi_client.get("/metakg")

    assert meta_knowledge_graph_response.status_code == 200
    assert metakg_response.status_code == 200
    assert metakg_response.json() == meta_knowledge_graph_response.json()
