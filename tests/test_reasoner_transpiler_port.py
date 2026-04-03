import pytest

from qlever_trapi import (
    BIOLINK_VOCAB,
    build_trapi_query,
    normalize_trapi_request,
    pascal_case,
    predicate_match_modes,
    snake_case,
    space_case,
)


CORRELATED_DESCENDANTS = {
    "biolink:correlated_with",
    "biolink:positively_correlated_with",
    "biolink:negatively_correlated_with",
    "biolink:occurs_together_in_literature_with",
    "biolink:coexpressed_with",
    "biolink:biomarker_for",
}
LIKELIHOOD_DESCENDANTS = {
    "biolink:affects_likelihood_of",
    "biolink:preventative_for_condition",
    "biolink:predisposes_to_condition",
}


def trapi_request(nodes: dict, edges: dict) -> dict:
    return {
        "message": {
            "query_graph": {
                "nodes": nodes,
                "edges": edges,
            }
        }
    }


def mode_map(predicates: list[str]) -> dict[bool, set[str]]:
    return {mode["reverse"]: set(mode["predicates"]) for mode in predicate_match_modes(predicates)}


def test_space_case_port() -> None:
    assert space_case("ChemicalSubstance") == "chemical substance"
    assert space_case(["ChemicalSubstance", "biological_process"]) == [
        "chemical substance",
        "biological process",
    ]
    with pytest.raises(ValueError):
        space_case({"a": "ChemicalSubstance"})


def test_snake_case_port() -> None:
    assert snake_case("ChemicalSubstance") == "chemical_substance"
    assert snake_case(["ChemicalSubstance", "Biological Process"]) == [
        "chemical_substance",
        "biological_process",
    ]
    with pytest.raises(ValueError):
        snake_case({"a": "ChemicalSubstance"})


def test_pascal_case_port() -> None:
    assert pascal_case("chemical_substance") == "ChemicalSubstance"
    assert pascal_case(["chemical_substance", "biological process"]) == [
        "ChemicalSubstance",
        "BiologicalProcess",
    ]
    with pytest.raises(ValueError):
        pascal_case({"a": "ChemicalSubstance"})


def test_invalid_node_constraint_value_port() -> None:
    request = trapi_request(
        {
            "n0": {
                "categories": "biolink:BiologicalEntity",
                "constraints": [
                    {
                        "id": "test:invalid_constraint",
                        "value": {"a": 1},
                    }
                ],
            }
        },
        {},
    )

    with pytest.raises(ValueError, match="Unsupported property type: dict"):
        build_trapi_query(normalize_trapi_request(request, subclass_depth=0))


def test_invalid_predicate_port() -> None:
    request = trapi_request(
        {
            "n0": {"ids": ["MONDO:0005148"]},
            "n1": {"categories": ["biolink:PhenotypicFeature"]},
        },
        {
            "e0": {
                "subject": "n0",
                "object": "n1",
                "predicates": ["biolink:invalid_predicate"],
            }
        },
    )

    with pytest.raises(ValueError, match="Invalid predicate in query"):
        build_trapi_query(normalize_trapi_request(request, subclass_depth=0))

    request["message"]["query_graph"]["edges"]["e0"]["predicates"] = [
        "biolink:invalid_predicate",
        "biolink:associated_with",
    ]
    with pytest.raises(ValueError, match="Invalid predicate in query"):
        build_trapi_query(normalize_trapi_request(request, subclass_depth=0))


def test_invalid_qualifier_port() -> None:
    request = trapi_request(
        {
            "n0": {},
            "n1": {"ids": ["NCBIGene:283871"]},
        },
        {
            "e0": {
                "subject": "n0",
                "object": "n1",
                "predicates": ["biolink:affects"],
                "qualifier_constraints": [
                    {
                        "qualifier_set": [
                            {
                                "qualifier_type_id": "bogus_qualifier_1",
                                "qualifier_value": "abundance",
                            }
                        ]
                    }
                ],
            }
        },
    )

    with pytest.raises(ValueError, match="Invalid qualifier in query"):
        build_trapi_query(normalize_trapi_request(request, subclass_depth=0))


def test_invalid_qualifier_value_port() -> None:
    request = trapi_request(
        {
            "n0": {},
            "n1": {"ids": ["NCBIGene:283871"]},
        },
        {
            "e0": {
                "subject": "n0",
                "object": "n1",
                "predicates": ["biolink:affects"],
                "qualifier_constraints": [
                    {
                        "qualifier_set": [
                            {
                                "qualifier_type_id": "biolink:object_aspect_qualifier",
                                "qualifier_value": "bogus_value",
                            }
                        ]
                    }
                ],
            }
        },
    )

    with pytest.raises(ValueError, match="Invalid value for qualifier object_aspect_qualifier"):
        build_trapi_query(normalize_trapi_request(request, subclass_depth=0))


def test_predicate_match_modes_symmetric_port() -> None:
    modes = mode_map(["biolink:correlated_with"])

    assert modes[False] == CORRELATED_DESCENDANTS
    assert modes[True] == CORRELATED_DESCENDANTS


def test_predicate_match_modes_directed_canonical_port() -> None:
    modes = predicate_match_modes(["biolink:affects"])

    assert modes == [
        {
            "reverse": False,
            "predicates": modes[0]["predicates"],
        }
    ]
    assert len(modes[0]["predicates"]) == 10
    assert "biolink:has_side_effect" in modes[0]["predicates"]
    assert "biolink:affects" in modes[0]["predicates"]


def test_predicate_match_modes_noncanonical_port() -> None:
    modes = predicate_match_modes(["biolink:affected_by"])

    assert modes == [
        {
            "reverse": True,
            "predicates": modes[0]["predicates"],
        }
    ]
    assert len(modes[0]["predicates"]) == 10
    assert "biolink:has_adverse_event" in modes[0]["predicates"]
    assert "biolink:has_side_effect" in modes[0]["predicates"]


def test_predicate_match_modes_multiple_canonical_port() -> None:
    modes = predicate_match_modes(["biolink:ameliorates_condition", "biolink:affects"])

    assert len(modes) == 1
    assert modes[0]["reverse"] is False
    assert len(modes[0]["predicates"]) == 10
    assert "biolink:ameliorates_condition" in modes[0]["predicates"]
    assert "biolink:affects" in modes[0]["predicates"]


def test_predicate_match_modes_multiple_noncanonical_port() -> None:
    modes = predicate_match_modes(
        ["biolink:condition_ameliorated_by", "biolink:likelihood_affected_by"]
    )

    assert modes == [
        {
            "reverse": True,
            "predicates": modes[0]["predicates"],
        }
    ]
    assert set(modes[0]["predicates"]) == {"biolink:ameliorates_condition"} | LIKELIHOOD_DESCENDANTS


def test_predicate_match_modes_multiple_conflicting_port() -> None:
    modes = mode_map(["biolink:ameliorates_condition", "biolink:likelihood_affected_by"])

    assert modes[False] == {"biolink:ameliorates_condition"}
    assert modes[True] == LIKELIHOOD_DESCENDANTS


def test_predicate_match_modes_symmetric_canonical_mix_port() -> None:
    modes = mode_map(["biolink:correlated_with", "biolink:affects_likelihood_of"])

    assert modes[False] == CORRELATED_DESCENDANTS | LIKELIHOOD_DESCENDANTS
    assert modes[True] == CORRELATED_DESCENDANTS


def test_predicate_match_modes_symmetric_noncanonical_mix_port() -> None:
    modes = mode_map(["biolink:correlated_with", "biolink:likelihood_affected_by"])

    assert modes[False] == CORRELATED_DESCENDANTS
    assert modes[True] == CORRELATED_DESCENDANTS | LIKELIHOOD_DESCENDANTS


def test_predicate_match_modes_related_to_and_no_predicate_port() -> None:
    assert predicate_match_modes(["biolink:related_to"]) == [
        {"reverse": False, "predicates": []},
        {"reverse": True, "predicates": []},
    ]
    assert predicate_match_modes([]) == [
        {"reverse": False, "predicates": []},
        {"reverse": True, "predicates": []},
    ]


def test_predicate_match_modes_related_to_overrides_specific_predicates_port() -> None:
    assert predicate_match_modes(["biolink:related_to", "biolink:treats"]) == [
        {"reverse": False, "predicates": []},
        {"reverse": True, "predicates": []},
    ]


def test_build_trapi_query_supports_multi_id_curie_lists_port() -> None:
    request = trapi_request(
        {
            "n0": {
                "ids": ["MONDO:0005148", "MONDO:0011122"],
                "categories": "biolink:Disease",
            },
            "n1": {
                "categories": "biolink:ChemicalSubstance",
            },
        },
        {
            "e01": {
                "subject": "n1",
                "object": "n0",
                "predicates": ["biolink:treats"],
            }
        },
    )

    query = build_trapi_query(normalize_trapi_request(request, subclass_depth=0))

    assert "<https://identifiers.org/MONDO:0005148>" in query
    assert "<https://identifiers.org/MONDO:0011122>" in query
    assert f"<{BIOLINK_VOCAB}Disease>" in query


def test_build_trapi_query_supports_inverse_predicate_port() -> None:
    request = trapi_request(
        {
            "n0": {"categories": "biolink:Disease"},
            "n1": {"categories": "biolink:PhenotypicFeature"},
        },
        {
            "e01": {
                "subject": "n0",
                "object": "n1",
                "predicates": "biolink:phenotype_of",
            }
        },
    )

    query = build_trapi_query(normalize_trapi_request(request, subclass_depth=0))

    assert f"<{BIOLINK_VOCAB}has_phenotype>" in query
    assert 'BIND("reverse" AS ?orientation_0_e01)' in query


def test_build_trapi_query_supports_predicate_lists_port() -> None:
    request = trapi_request(
        {
            "n0": {"categories": "biolink:Disease"},
            "n1": {"categories": "biolink:PhenotypicFeature"},
        },
        {
            "e01": {
                "subject": "n0",
                "object": "n1",
                "predicates": ["biolink:capable_of", "biolink:biomarker_for"],
            }
        },
    )

    query = build_trapi_query(normalize_trapi_request(request, subclass_depth=0))

    assert f"<{BIOLINK_VOCAB}capable_of>" in query
    assert f"<{BIOLINK_VOCAB}biomarker_for>" in query


def test_build_trapi_query_supports_single_predicate_list_port() -> None:
    request = trapi_request(
        {
            "n0": {"categories": "biolink:Disease"},
            "n1": {"categories": "biolink:PhenotypicFeature"},
        },
        {
            "e01": {
                "subject": "n0",
                "object": "n1",
                "predicates": ["biolink:capable_of"],
            }
        },
    )

    query = build_trapi_query(normalize_trapi_request(request, subclass_depth=0))

    assert f"<{BIOLINK_VOCAB}capable_of>" in query


def test_normalize_trapi_request_accepts_integer_ids_port() -> None:
    normalized = normalize_trapi_request(
        trapi_request(
            {
                "n0": {
                    "ids": 12,
                    "categories": "biolink:Disease",
                }
            },
            {},
        ),
        subclass_depth=0,
    )

    assert normalized["original_qnodes"]["n0"]["ids"] == ["12"]


def test_normalize_trapi_request_defaults_missing_categories_to_named_thing_port() -> None:
    normalized = normalize_trapi_request(
        trapi_request({"n0": {"ids": ["MONDO:0014488"]}}, {}),
        subclass_depth=0,
    )

    assert normalized["original_qnodes"]["n0"]["categories"] == ["biolink:NamedThing"]


def test_normalize_trapi_request_accepts_relation_none_port() -> None:
    normalized = normalize_trapi_request(
        trapi_request(
            {"n0": {}, "n1": {}},
            {
                "e0": {
                    "subject": "n0",
                    "object": "n1",
                    "relation": None,
                }
            },
        ),
        subclass_depth=0,
    )

    assert normalized["original_qedges"]["e0"]["predicates"] == []


def test_normalize_trapi_request_ignores_null_qnode_properties_port() -> None:
    normalized = normalize_trapi_request(
        trapi_request(
            {
                "n0": {
                    "ids": ["MONDO:0014488"],
                    "chromosome": None,
                }
            },
            {},
        ),
        subclass_depth=0,
    )

    assert normalized["original_qnodes"]["n0"]["ids"] == ["MONDO:0014488"]


def test_normalize_trapi_request_accepts_predicate_none_port() -> None:
    normalized = normalize_trapi_request(
        trapi_request(
            {
                "n0": {"categories": "biolink:Disease"},
                "n1": {"categories": "biolink:Gene"},
            },
            {
                "e0": {
                    "subject": "n0",
                    "object": "n1",
                    "predicates": None,
                }
            },
        ),
        subclass_depth=0,
    )

    assert normalized["original_qedges"]["e0"]["predicates"] == []


def test_normalize_trapi_request_preserves_empty_constraint_lists_port() -> None:
    normalized = normalize_trapi_request(
        trapi_request(
            {
                "n0": {
                    "categories": "biolink:Gene",
                    "constraints": [],
                },
                "n1": {
                    "constraints": [],
                },
            },
            {
                "e0": {
                    "subject": "n0",
                    "object": "n1",
                    "attribute_constraints": [],
                }
            },
        ),
        subclass_depth=0,
    )

    assert normalized["original_qnodes"]["n0"]["constraints"] == []
    assert normalized["original_qnodes"]["n1"]["constraints"] == []
    assert normalized["original_qedges"]["e0"]["attribute_constraints"] == []


def test_build_trapi_query_sanitizes_unusual_qgraph_keys_port() -> None:
    query = build_trapi_query(
        normalize_trapi_request(
            trapi_request(
                {
                    "type-2 diabetes": {
                        "categories": "biolink:Disease",
                    },
                    "n1": {
                        "categories": "biolink:Gene",
                    },
                },
                {
                    "interacts with": {
                        "subject": "type-2 diabetes",
                        "object": "n1",
                    }
                },
            ),
            subclass_depth=0,
        )
    )

    assert "?node_0_type_2_diabetes" in query
    assert "?edge_0_interacts_with" in query


def test_normalize_trapi_request_skips_subclass_rewrite_for_node_only_queries_port() -> None:
    normalized = normalize_trapi_request(
        trapi_request({"n0": {"ids": ["MONDO:0000001"]}}, {}),
        subclass_depth=1,
    )

    assert set(normalized["qnodes"]) == {"n0"}
    assert normalized["qedges"] == {}


def test_normalize_trapi_request_skips_subclass_rewrite_for_explicit_hierarchy_queries_port() -> None:
    normalized = normalize_trapi_request(
        trapi_request(
            {
                "n0": {"ids": ["MONDO:0000001"]},
                "n1": {},
            },
            {
                "e0": {
                    "subject": "n0",
                    "object": "n1",
                    "predicates": ["biolink:superclass_of"],
                }
            },
        ),
        subclass_depth=1,
    )

    assert set(normalized["qnodes"]) == {"n0", "n1"}
    assert set(normalized["qedges"]) == {"e0"}


def test_normalize_trapi_request_skips_subclass_rewrite_for_explicit_subclass_queries_port() -> None:
    normalized = normalize_trapi_request(
        trapi_request(
            {
                "n0": {"ids": ["MONDO:0005148"]},
                "n1": {},
            },
            {
                "e0": {
                    "subject": "n0",
                    "object": "n1",
                    "predicates": ["biolink:subclass_of"],
                }
            },
        ),
        subclass_depth=1,
    )

    assert set(normalized["qnodes"]) == {"n0", "n1"}
    assert set(normalized["qedges"]) == {"e0"}


def test_normalize_trapi_request_moves_categories_to_superclass_nodes_port() -> None:
    normalized = normalize_trapi_request(
        trapi_request(
            {
                "n0": {
                    "ids": ["HP:0011015"],
                    "categories": ["biolink:PhenotypicFeature"],
                },
                "n1": {},
            },
            {
                "e0": {
                    "subject": "n1",
                    "object": "n0",
                    "predicates": ["biolink:treats"],
                }
            },
        ),
        subclass_depth=1,
    )

    assert normalized["qnodes"]["n0"]["ids"] == []
    assert normalized["qnodes"]["n0"]["categories"] == []
    assert normalized["qnodes"]["n0_superclass"]["ids"] == ["HP:0011015"]
    assert normalized["qnodes"]["n0_superclass"]["categories"] == ["biolink:PhenotypicFeature"]


def test_normalize_trapi_request_uses_requested_subclass_depth_port() -> None:
    normalized = normalize_trapi_request(
        trapi_request(
            {
                "n0": {"ids": ["MONDO:0000001"]},
                "n1": {},
            },
            {
                "e0": {
                    "subject": "n0",
                    "object": "n1",
                }
            },
        ),
        subclass_depth=2,
    )

    assert normalized["qedges"]["n0_subclass_edge"]["_max_path_length"] == 2
    query = build_trapi_query(normalized)
    assert "?edge_1_n0_subclass_edge_hop_1" in query


def test_invalid_subclass_depth_port() -> None:
    request = trapi_request(
        {
            "n0": {"ids": ["CHEBI:136043"]},
            "n1": {"ids": ["MONDO:0000000"]},
        },
        {
            "e01": {
                "subject": "n0",
                "object": "n1",
                "predicates": ["biolink:treats"],
            }
        },
    )

    with pytest.raises(TypeError):
        normalize_trapi_request(request, subclass_depth="bad_value_type")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="subclass_depth must be non-negative"):
        normalize_trapi_request(request, subclass_depth=-1)
