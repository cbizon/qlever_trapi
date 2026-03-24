from benchmark_find_paths import extract_summary


def test_extract_summary_reads_footer_values(tmp_path):
    path = tmp_path / "result.json"
    path.write_text(
        """{
  "paths": [],
  "path_count": 7336614,
  "path_length": 3,
  "query_time_ms": 79251,
  "skipped_malformed_rows": 0,
  "start": "https://identifiers.org/CHEBI:45783"
}
""",
        encoding="utf-8",
    )

    assert extract_summary(path) == {
        "path_count": 7336614,
        "query_time_ms": 79251,
        "skipped_malformed_rows": 0,
    }
