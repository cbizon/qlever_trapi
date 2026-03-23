#!/usr/bin/env bash
set -euo pipefail

INPUT_ARCHIVE="inputs/translator_March19_2026/translator_kg.tar.zst"
DATASET_NAME="translator_kg"
ARTIFACTS_DIR="artifacts"
OUTPUT_RDF="${ARTIFACTS_DIR}/rdf/${DATASET_NAME}.nt.zst"
DATASET_BASE="${ARTIFACTS_DIR}/qlever/${DATASET_NAME}/${DATASET_NAME}"
STXXL_MEMORY="32G"

mkdir -p "$(dirname "$OUTPUT_RDF")" "$(dirname "$DATASET_BASE")"

uv run python kgx_to_qlever_rdf.py "$INPUT_ARCHIVE" "$OUTPUT_RDF"

qlever index \
  --system native \
  --name "$DATASET_BASE" \
  --format nt \
  --input-files "$OUTPUT_RDF" \
  --cat-input-files "zstd -dc -- $OUTPUT_RDF" \
  --parallel-parsing false \
  --text-index from_literals \
  --stxxl-memory "$STXXL_MEMORY"

# Start QLever with:
# qlever start --system native --name "$DATASET_BASE" --memory-for-queries 16G --timeout 120s
