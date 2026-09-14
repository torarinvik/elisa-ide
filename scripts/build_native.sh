#!/usr/bin/env bash
# Build the designer's native (SDL3) application with the resolved stage1 product.
#
# Usage:
#   scripts/build_native.sh [entry.elisa] [output-name]
#
# Defaults to src/app/main_native.elisa -> build/elisa_ide. Records the
# resolved dependency/compiler provenance to build/provenance.json first.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE1="${ELISA_UI_STAGE1:-$ROOT/../wasm-sdk-compiler}"
SDL_LIB="${ELISA_UI_SDL_LIB:-/opt/homebrew/lib}"
ENTRY="${1:-$ROOT/src/app/main_native.elisa}"
NAME="${2:-elisa_ide}"

if [[ "$ENTRY" != /* ]]; then
    ENTRY="$ROOT/$ENTRY"
fi

[[ -x "$STAGE1/bin/elisac-stage1" ]] || { echo "build: no stage1 product under $STAGE1 (run scripts/doctor.sh for details)" >&2; exit 2; }
[[ -f "$ENTRY" ]] || { echo "build: no entry source at $ENTRY" >&2; exit 2; }

mkdir -p "$ROOT/build"
bash "$ROOT/scripts/doctor.sh" --json > "$ROOT/build/provenance.json"

# The native designer shell hosts the core document session in its own
# process. Build that sibling executable first so a successful shell build is
# runnable from the repository root without relying on a stale host binary.
if [[ "$ENTRY" == "$ROOT/src/app/main_native.elisa" ]]; then
    bash "$ROOT/scripts/build_core.sh" "$ROOT/worker/core/document_host.elisa" document_host >/dev/null
    bash "$ROOT/scripts/build_preview_worker.sh" >/dev/null
fi

SHIM_SOURCE="$ROOT/src/platform/posix/designer_posix.c"
SHIM_OBJECT="$ROOT/build/designer_posix.o"
if [[ -f "$SHIM_SOURCE" ]]; then
    "${ELISA_UI_CC:-clang}" -std=c11 -O2 -c -o "$SHIM_OBJECT" "$SHIM_SOURCE"
fi

bash "$STAGE1/scripts/elisac_stage1.sh" -O0 -o "$ROOT/build/$NAME.o" "$ENTRY"
LINK_INPUTS=("$ROOT/build/$NAME.o" "$STAGE1/build/runtime/elisacore_runtime.o")
[[ -f "$SHIM_OBJECT" ]] && LINK_INPUTS+=("$SHIM_OBJECT")
LINK_FLAGS=(-Wl,-dead_strip)
if [[ "$(uname -s)" == "Darwin" ]]; then
    LINK_FLAGS+=(-framework CoreGraphics -framework ImageIO)
fi
clang "${LINK_FLAGS[@]}" -o "$ROOT/build/$NAME" \
    "${LINK_INPUTS[@]}" \
    -L"$SDL_LIB" -lSDL3 -lSDL3_ttf -Wl,-rpath,"$SDL_LIB"
echo "built $ROOT/build/$NAME"
