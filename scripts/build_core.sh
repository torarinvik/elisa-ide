#!/usr/bin/env bash
# Build a portable core unit (model, schema, registry, validation, CLI).
#
# Core units include the runtime standard library and never the elisa-ui
# framework (ADR-001). They link only the runtime object, so core tests run
# without SDL or a window.
#
# Usage:
#   scripts/build_core.sh <entry.elisa> <output-name>
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE1="${ELISA_UI_STAGE1:-$ROOT/../wasm-sdk-compiler}"
RUNTIME="$STAGE1/build/runtime/elisacore_runtime.o"
ENTRY="$1"
NAME="$2"

if [[ "$ENTRY" != /* ]]; then
    ENTRY="$ROOT/$ENTRY"
fi

[[ -x "$STAGE1/bin/elisac-stage1" ]] || { echo "build_core: no stage1 product under $STAGE1 (run scripts/doctor.sh)" >&2; exit 2; }
[[ -f "$RUNTIME" ]] || { echo "build_core: no runtime object at $RUNTIME" >&2; exit 2; }
[[ -f "$ENTRY" ]] || { echo "build_core: no entry source at $ENTRY" >&2; exit 2; }

mkdir -p "$ROOT/build"
bash "$ROOT/scripts/doctor.sh" --json > "$ROOT/build/provenance-core-$NAME.json"
SHIM_SOURCE="$ROOT/src/platform/posix/designer_posix.c"
SHIM_OBJECT="$ROOT/build/designer_posix.o"
if [[ -f "$SHIM_SOURCE" ]]; then
    "${ELISA_UI_CC:-clang}" -std=c11 -O2 -c -o "$SHIM_OBJECT" "$SHIM_SOURCE"
fi
bash "$STAGE1/scripts/elisac_stage1.sh" -O0 -o "$ROOT/build/$NAME.o" "$ENTRY"
LINK_INPUTS=("$ROOT/build/$NAME.o" "$RUNTIME")
[[ -f "$SHIM_OBJECT" ]] && LINK_INPUTS+=("$SHIM_OBJECT")
clang -Wl,-dead_strip -o "$ROOT/build/$NAME" "${LINK_INPUTS[@]}"
echo "built $ROOT/build/$NAME"
