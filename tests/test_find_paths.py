import pytest

from find_paths import build_paths_query, build_properties_query, format_path_row, parse_args


def test_build_paths_query_length_two_projects_triples_for_each_hop():
    query = build_paths_query(
        "https://identifiers.org/CHEBI:45783",
        "https://identifiers.org/MONDO:0004979",
        2,
        limit=50,
    )
    assert "VALUES" not in query
    assert "?subject1 ?predicate1 ?object1 ?subject2 ?predicate2 ?object2" in query
    assert "<https://identifiers.org/CHEBI:45783>" in query
    assert "<https://identifiers.org/MONDO:0004979>" in query
    assert "?dir1" not in query
    assert "?edge1" in query
    assert "FILTER(?edge1 != ?edge2)" not in query
    assert query.count("UNION") == 3
    assert "LIMIT 50" in query


def test_build_paths_query_keeps_endpoint_constants_inline_in_base_mode():
    query = build_paths_query(
        "https://identifiers.org/CHEBI:45783",
        "https://identifiers.org/MONDO:0004979",
        3,
    )
    assert "rdf:subject <https://identifiers.org/CHEBI:45783>" in query
    assert "rdf:object <https://identifiers.org/CHEBI:45783>" in query
    assert "rdf:subject <https://identifiers.org/MONDO:0004979>" in query
    assert "rdf:object <https://identifiers.org/MONDO:0004979>" in query


def test_build_paths_query_length_three_projects_three_triples():
    query = build_paths_query(
        "https://identifiers.org/A:1",
        "https://identifiers.org/B:1",
        3,
    )
    assert query.count("UNION") == 7
    assert "?node1" in query
    assert "?node2" in query
    assert "?subject1 ?predicate1 ?object1" in query
    assert "?subject2 ?predicate2 ?object2" in query
    assert "?subject3 ?predicate3 ?object3" in query
    assert "FILTER(?edge1 != ?edge2)" not in query
    assert "FILTER(?edge1 != ?edge3)" not in query
    assert "FILTER(?edge2 != ?edge3)" not in query


def test_build_paths_query_with_subclasses_is_not_implemented_in_file_backed_mode():
    with pytest.raises(NotImplementedError):
        build_paths_query(
            "https://identifiers.org/CHEBI:45783",
            "https://identifiers.org/MONDO:0004979",
            2,
            include_subclasses=True,
        )


def test_build_paths_query_traversal_mode_uses_traversal_predicates_without_unions():
    query = build_paths_query(
        "https://identifiers.org/A:1",
        "https://identifiers.org/B:1",
        3,
        query_mode="traversal",
    )
    assert "<https://w3id.org/kgx/traversal/traversal_from>" in query
    assert "<https://w3id.org/kgx/traversal/traversal_to>" in query
    assert "<https://w3id.org/kgx/traversal/traverses>" not in query
    assert "?subject1 ?predicate1 ?object1" in query
    assert "?subject2 ?predicate2 ?object2" in query
    assert "?subject3 ?predicate3 ?object3" in query
    assert query.count("UNION") == 0


def test_build_properties_query_excludes_reification_predicates():
    query = build_properties_query(
        ["https://identifiers.org/CHEBI:45783", "urn:uuid:test-edge"]
    )
    assert "VALUES ?resource" in query
    assert "rdf:subject" not in query
    assert "<http://www.w3.org/1999/02/22-rdf-syntax-ns#subject>" in query


def test_parse_args_defaults_page_size_to_100k(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["find_paths.py", "CHEBI:45783", "MONDO:0004979", "3"],
    )
    args = parse_args()
    assert args.page_size == 100000
    assert args.include_subclasses is False
    assert args.query_mode == "original"


def test_format_path_row_uses_projected_subject_predicate_object_columns():
    row = {
        "?subject1": "https://identifiers.org/A:1",
        "?predicate1": "https://w3id.org/biolink/vocab/related_to",
        "?object1": "https://identifiers.org/B:1",
        "?subject2": "https://identifiers.org/B:1",
        "?predicate2": "https://w3id.org/biolink/vocab/treats",
        "?object2": "https://identifiers.org/C:1",
    }

    assert format_path_row(row, 2) == {
        "steps": [
            {
                "subject": "https://identifiers.org/A:1",
                "predicate": "https://w3id.org/biolink/vocab/related_to",
                "object": "https://identifiers.org/B:1",
            },
            {
                "subject": "https://identifiers.org/B:1",
                "predicate": "https://w3id.org/biolink/vocab/treats",
                "object": "https://identifiers.org/C:1",
            },
        ]
    }
