#!/usr/bin/env bash
set -euo pipefail

INPUT_ARCHIVE="${1:-translator_kg.tar.zst}"
OUTPUT_RDF="${2:-translator_kg.nt.zst}"
DATASET_NAME="${3:-translator_kg}"
STXXL_MEMORY="${STXXL_MEMORY:-32G}"

uv run python kgx_to_qlever_rdf.py "$INPUT_ARCHIVE" "$OUTPUT_RDF"

qlever index \
  --system native \
  --name "$DATASET_NAME" \
  --format nt \
  --input-files "$OUTPUT_RDF" \
  --cat-input-files "zstd -dc -- $OUTPUT_RDF" \
  --parallel-parsing false \
  --text-index from_literals \
  --stxxl-memory "$STXXL_MEMORY"
