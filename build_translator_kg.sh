#!/usr/bin/env bash
set -euo pipefail

INPUT_ARCHIVE="inputs/translator_March19_2026/translator_kg.tar.zst"
ARTIFACTS_DIR="artifacts"
ADD_REVERSE_TRAVERSAL_EDGES="true"
DATASET_NAME="translator_kg"

if [[ "$ADD_REVERSE_TRAVERSAL_EDGES" == "true" ]]; then
  OUTPUT_DATASET_NAME="${DATASET_NAME}_reverse_traversal"
else
  OUTPUT_DATASET_NAME="$DATASET_NAME"
fi

OUTPUT_RDF="${ARTIFACTS_DIR}/rdf/${OUTPUT_DATASET_NAME}.nt.zst"
DATASET_BASE="${ARTIFACTS_DIR}/qlever/${OUTPUT_DATASET_NAME}/${OUTPUT_DATASET_NAME}"
STXXL_MEMORY="32G"

EXTRA_CONVERTER_ARGS=()
if [[ "$ADD_REVERSE_TRAVERSAL_EDGES" == "true" ]]; then
  EXTRA_CONVERTER_ARGS+=(--add-reverse-traversal-edges)
fi

mkdir -p "$(dirname "$OUTPUT_RDF")" "$(dirname "$DATASET_BASE")"

if [[ ${#EXTRA_CONVERTER_ARGS[@]} -gt 0 ]]; then
  uv run python kgx_to_qlever_rdf.py "$INPUT_ARCHIVE" "$OUTPUT_RDF" "${EXTRA_CONVERTER_ARGS[@]}"
else
  uv run python kgx_to_qlever_rdf.py "$INPUT_ARCHIVE" "$OUTPUT_RDF"
fi

uv run qlever index \
  --system native \
  --name "$DATASET_BASE" \
  --format nt \
  --input-files "$OUTPUT_RDF" \
  --cat-input-files "zstd -dc -- $OUTPUT_RDF" \
  --overwrite-existing \
  --parallel-parsing true \
  --text-index from_literals \
  --stxxl-memory "$STXXL_MEMORY"

echo
echo "Build complete."
echo "To start or restart QLever for this dataset:"
echo "  uv run qlever start --system native --name \"$DATASET_BASE\" --memory-for-queries 20G --timeout 300s"
