#!/usr/bin/env python3
import argparse
import csv
import json
import re
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from find_paths import (
    build_count_query,
    build_paths_query,
    curie_to_iri,
    format_path_row,
    run_qlever_query,
)

SUMMARY_PATTERNS = {
    "path_count": re.compile(r'"path_count"\s*:\s*(\d+)'),
    "query_time_ms": re.compile(r'"query_time_ms"\s*:\s*(\d+)'),
    "skipped_malformed_rows": re.compile(r'"skipped_malformed_rows"\s*:\s*(\d+)'),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark full-result retrieval from find_paths.py across page sizes."
    )
    parser.add_argument("start_curie", help="Start CURIE, for example CHEBI:45783")
    parser.add_argument("end_curie", help="End CURIE, for example MONDO:0004979")
    parser.add_argument(
        "--path-lengths",
        nargs="+",
        type=int,
        default=[2, 3],
        help="Path lengths to benchmark. Default: 2 3",
    )
    parser.add_argument(
        "--page-sizes",
        nargs="+",
        type=int,
        default=[100000, 500000, 1000000, 10000000],
        help="Page sizes to benchmark. Default: 100000 500000 1000000 10000000",
    )
    parser.add_argument("--host-name", default="localhost", help="QLever host. Default: localhost")
    parser.add_argument("--port", type=int, default=8888, help="QLever port. Default: 8888")
    parser.add_argument(
        "--output",
        default="artifacts/benchmarks/find_paths_page_sizes.json",
        help="Where to write benchmark results.",
    )
    parser.add_argument(
        "--keep-results",
        action="store_true",
        help="Keep the raw JSON result files from each benchmark run.",
    )
    parser.add_argument(
        "--query-modes",
        nargs="+",
        choices=["original", "traversal"],
        default=["original", "traversal"],
        help="Query modes to benchmark. Default: original traversal",
    )
    return parser.parse_args()


def tail_text(path: Path, max_bytes: int = 256 * 1024) -> str:
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > max_bytes:
            handle.seek(-max_bytes, 2)
        return handle.read().decode("utf-8")


def extract_summary(path: Path) -> dict[str, int]:
    text = tail_text(path)
    summary: dict[str, int] = {}
    for key, pattern in SUMMARY_PATTERNS.items():
        match = pattern.search(text)
        if not match:
            raise ValueError(f"Could not find {key} in summary tail of {path}")
        summary[key] = int(match.group(1))
    return summary


def stream_tsv_to_file(
    host_name: str,
    port: int,
    query: str,
    output_path: Path,
) -> dict[str, float | int]:
    data = urllib.parse.urlencode({"query": query}).encode("utf-8")
    request = urllib.request.Request(
        f"http://{host_name}:{port}",
        data=data,
        headers={"Accept": "text/tab-separated-values"},
        method="POST",
    )
    row_count = 0
    response_read_wall_time_s = 0.0
    output_write_wall_time_s = 0.0
    with urllib.request.urlopen(request) as response, output_path.open("wb") as output_handle:
        response_iter = iter(response)
        first = True
        while True:
            read_start = time.perf_counter()
            try:
                raw_line = next(response_iter)
            except StopIteration:
                break
            response_read_wall_time_s += time.perf_counter() - read_start
            write_start = time.perf_counter()
            output_handle.write(raw_line)
            output_write_wall_time_s += time.perf_counter() - write_start
            if first:
                first = False
                continue
            if raw_line.strip():
                row_count += 1
    return {
        "row_count": row_count,
        "response_read_wall_time_s": round(response_read_wall_time_s, 3),
        "output_write_wall_time_s": round(output_write_wall_time_s, 3),
    }


def write_json_from_tsv(tsv_path: Path, json_path: Path, path_length: int, start_iri: str, end_iri: str) -> dict[str, int]:
    path_count = 0
    with tsv_path.open("r", encoding="utf-8", newline="") as input_handle, json_path.open("w", encoding="utf-8") as output_handle:
        reader = csv.reader(input_handle, delimiter="\t")
        try:
            header = next(reader)
        except StopIteration:
            header = []
        output_handle.write("{\n")
        output_handle.write(f'  "end": {json.dumps(end_iri)},\n')
        output_handle.write('  "paths": [\n')
        first = True
        for values in reader:
            if not values:
                continue
            row = {column: value for column, value in zip(header, values, strict=False)}
            path = format_path_row(row, path_length)
            if not first:
                output_handle.write(",\n")
            output_handle.write(json.dumps(path, indent=4, sort_keys=True))
            first = False
            path_count += 1
        output_handle.write("\n  ],\n")
        output_handle.write(f'  "path_count": {path_count},\n')
        output_handle.write(f'  "path_length": {path_length},\n')
        output_handle.write('  "query_time_ms": 0,\n')
        output_handle.write('  "skipped_malformed_rows": 0,\n')
        output_handle.write(f'  "start": {json.dumps(start_iri)}\n')
        output_handle.write("}\n")
    return {"path_count": path_count, "skipped_malformed_rows": 0}


def benchmark_one(
    start_curie: str,
    end_curie: str,
    path_length: int,
    page_size: int,
    query_mode: str,
    host_name: str,
    port: int,
    result_path: Path,
) -> dict[str, object]:
    start_iri = curie_to_iri(start_curie)
    end_iri = curie_to_iri(end_curie)
    paths_query = build_paths_query(
        start_iri,
        end_iri,
        path_length,
        query_mode=query_mode,
    )
    count_query = build_count_query(paths_query)
    count_start = time.perf_counter()
    count_result = run_qlever_query(host_name, port, count_query)
    count_wall_time_s = round(time.perf_counter() - count_start, 3)
    count_match = re.search(r"\n\"?(\\d+)\"?\s*$", count_result["payload"])
    if count_match is None:
        count_match = re.search(r"(\d+)", count_result["payload"])
    if count_match is None:
        raise ValueError("Could not parse count result")
    count_value = int(count_match.group(1))

    tsv_path = result_path.with_suffix(".tsv")
    retrieve_start = time.perf_counter()
    retrieved_rows = 0
    offset = 0
    response_read_wall_time_s = 0.0
    temp_tsv_write_wall_time_s = 0.0
    temp_tsv_read_wall_time_s = 0.0
    with tsv_path.open("wb") as tsv_handle:
        first_page = True
        while True:
            page_query = build_paths_query(
                start_iri,
                end_iri,
                path_length,
                limit=page_size,
                offset=offset,
                query_mode=query_mode,
            )
            page_path = tsv_path.with_suffix(f".{offset}.page.tsv")
            page_result = stream_tsv_to_file(host_name, port, page_query, page_path)
            with page_path.open("rb") as page_handle:
                if first_page:
                    read_start = time.perf_counter()
                    page_bytes = page_handle.read()
                    temp_tsv_read_wall_time_s += time.perf_counter() - read_start
                    write_start = time.perf_counter()
                    tsv_handle.write(page_bytes)
                    temp_tsv_write_wall_time_s += time.perf_counter() - write_start
                    first_page = False
                else:
                    read_start = time.perf_counter()
                    page_handle.readline()
                    page_bytes = page_handle.read()
                    temp_tsv_read_wall_time_s += time.perf_counter() - read_start
                    write_start = time.perf_counter()
                    tsv_handle.write(page_bytes)
                    temp_tsv_write_wall_time_s += time.perf_counter() - write_start
            page_path.unlink()
            retrieved_rows += int(page_result["row_count"])
            response_read_wall_time_s += float(page_result["response_read_wall_time_s"])
            temp_tsv_write_wall_time_s += float(page_result["output_write_wall_time_s"])
            if int(page_result["row_count"]) < page_size:
                break
            offset += int(page_result["row_count"])
    retrieve_wall_time_s = round(time.perf_counter() - retrieve_start, 3)

    write_start = time.perf_counter()
    write_summary = write_json_from_tsv(tsv_path, result_path, path_length, start_iri, end_iri)
    write_wall_time_s = round(time.perf_counter() - write_start, 3)
    wall_time_s = round(retrieve_wall_time_s + write_wall_time_s, 3)
    tsv_size_bytes = tsv_path.stat().st_size
    path_count = write_summary["path_count"]
    skipped_malformed_rows = write_summary["skipped_malformed_rows"]
    tsv_path.unlink()
    return {
        "path_length": path_length,
        "page_size": page_size,
        "query_mode": query_mode,
        "count_value": count_value,
        "count_query_time_ms": count_result["elapsed_ms"],
        "count_wall_time_s": count_wall_time_s,
        "retrieved_rows": retrieved_rows,
        "response_read_wall_time_s": round(response_read_wall_time_s, 3),
        "temp_tsv_write_wall_time_s": round(temp_tsv_write_wall_time_s, 3),
        "temp_tsv_read_wall_time_s": round(temp_tsv_read_wall_time_s, 3),
        "retrieve_wall_time_s": retrieve_wall_time_s,
        "retrieve_size_bytes": tsv_size_bytes,
        "write_wall_time_s": write_wall_time_s,
        "wall_time_s": wall_time_s,
        "result_size_bytes": result_path.stat().st_size,
        "path_count": path_count,
        "skipped_malformed_rows": skipped_malformed_rows,
    }


def main() -> None:
    args = parse_args()
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    results = {
        "start_curie": args.start_curie,
        "end_curie": args.end_curie,
        "host_name": args.host_name,
        "port": args.port,
        "path_lengths": args.path_lengths,
        "page_sizes": args.page_sizes,
        "query_modes": args.query_modes,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "benchmarks": [],
    }

    if args.keep_results:
        work_dir = output_path.parent / "find_paths_page_size_results"
        work_dir.mkdir(parents=True, exist_ok=True)
        temp_dir_cm = None
    else:
        temp_dir_cm = tempfile.TemporaryDirectory(prefix="find_paths_bench_")
        work_dir = Path(temp_dir_cm.__enter__())

    try:
        for path_length in args.path_lengths:
            for query_mode in args.query_modes:
                for page_size in args.page_sizes:
                    result_path = work_dir / f"{query_mode}_{path_length}hop_{page_size}.json"
                    benchmark = benchmark_one(
                        args.start_curie,
                        args.end_curie,
                        path_length,
                        page_size,
                        query_mode,
                        args.host_name,
                        args.port,
                        result_path,
                    )
                    if args.keep_results:
                        benchmark["result_path"] = str(result_path)
                    else:
                        result_path.unlink()
                    results["benchmarks"].append(benchmark)
        output_path.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(output_path)
        print(json.dumps(results, indent=2, sort_keys=True))
    finally:
        if temp_dir_cm is not None:
            temp_dir_cm.__exit__(None, None, None)


if __name__ == "__main__":
    main()
