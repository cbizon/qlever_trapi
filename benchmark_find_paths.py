#!/usr/bin/env python3
import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path


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


def benchmark_one(
    start_curie: str,
    end_curie: str,
    path_length: int,
    page_size: int,
    host_name: str,
    port: int,
    result_path: Path,
) -> dict[str, object]:
    command = [
        sys.executable,
        "find_paths.py",
        start_curie,
        end_curie,
        str(path_length),
        "--host-name",
        host_name,
        "--port",
        str(port),
        "--page-size",
        str(page_size),
    ]
    start = time.perf_counter()
    with result_path.open("w", encoding="utf-8") as output_handle:
        subprocess.run(command, check=True, stdout=output_handle)
    wall_time_s = round(time.perf_counter() - start, 3)
    summary = extract_summary(result_path)
    return {
        "path_length": path_length,
        "page_size": page_size,
        "wall_time_s": wall_time_s,
        "result_size_bytes": result_path.stat().st_size,
        **summary,
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
            for page_size in args.page_sizes:
                result_path = work_dir / f"{path_length}hop_{page_size}.json"
                benchmark = benchmark_one(
                    args.start_curie,
                    args.end_curie,
                    path_length,
                    page_size,
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
