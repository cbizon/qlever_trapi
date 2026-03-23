from find_paths import build_paths_query, build_properties_query, parse_args


def test_build_paths_query_length_two_inlines_constants_and_directions():
    query = build_paths_query(
        "https://identifiers.org/CHEBI:45783",
        "https://identifiers.org/MONDO:0004979",
        2,
        limit=50,
    )
    assert "VALUES" not in query
    assert 'BIND("forward" AS ?dir1)' in query
    assert 'BIND("reverse" AS ?dir2)' in query
    assert "<https://identifiers.org/CHEBI:45783>" in query
    assert "<https://identifiers.org/MONDO:0004979>" in query
    assert "node1_label" not in query
    assert "FILTER(?edge1 != ?edge2)" not in query
    assert "LIMIT 50" in query


def test_build_paths_query_length_three_has_all_branch_directions():
    query = build_paths_query(
        "https://identifiers.org/A:1",
        "https://identifiers.org/B:1",
        3,
    )
    assert query.count("UNION") == 7
    assert "?node1" in query
    assert "?node2" in query
    assert "FILTER(?edge1 != ?edge2)" not in query
    assert "FILTER(?edge1 != ?edge3)" not in query
    assert "FILTER(?edge2 != ?edge3)" not in query


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
