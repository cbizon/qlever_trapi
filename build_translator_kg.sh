#!/usr/bin/env bash
set -euo pipefail

INPUT_ARCHIVE="inputs/translator_March19_2026/translator_kg.tar.zst"
DATASET_NAME="translator_kg_reverse_traversal"
ARTIFACTS_DIR="artifacts"
ADD_REVERSE_TRAVERSAL_EDGES="true"
OUTPUT_RDF="${ARTIFACTS_DIR}/rdf/${DATASET_NAME}.nt.zst"
DATASET_BASE="${ARTIFACTS_DIR}/qlever/${DATASET_NAME}/${DATASET_NAME}"
STXXL_MEMORY="32G"

EXTRA_CONVERTER_ARGS=()
if [[ "$ADD_REVERSE_TRAVERSAL_EDGES" == "true" ]]; then
  EXTRA_CONVERTER_ARGS+=(--add-reverse-traversal-edges)
fi

mkdir -p "$(dirname "$OUTPUT_RDF")" "$(dirname "$DATASET_BASE")"

uv run python kgx_to_qlever_rdf.py "$INPUT_ARCHIVE" "$OUTPUT_RDF" "${EXTRA_CONVERTER_ARGS[@]}"

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
qlever start --system native --name "$DATASET_BASE" --memory-for-queries 20G --timeout 300s
