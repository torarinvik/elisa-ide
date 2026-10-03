#!/usr/bin/env bash
# Build an SDL3 native harness or generated-app target with the resolved stage1 product.
#
# Usage:
#   scripts/build_native.sh <entry.elisa> <output-name>
#
# The IDE desktop entrypoint is AppKit + Skia and must be built with
# scripts/build_ide.sh. This script is intentionally limited to SDL3 harnesses
# and generated-app profiles.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE1_DEFAULT="$ROOT/compiler"
[[ -d "$STAGE1_DEFAULT" ]] || STAGE1_DEFAULT="$ROOT/../Elisa-compiler"
STAGE1="${ELISA_UI_STAGE1:-$STAGE1_DEFAULT}"
SDL_LIB="${ELISA_UI_SDL_LIB:-/opt/homebrew/lib}"
ENTRY="${1:?usage: scripts/build_native.sh <entry.elisa> <output-name>; use scripts/build_ide.sh for the IDE}"
NAME="${2:?usage: scripts/build_native.sh <entry.elisa> <output-name>; use scripts/build_ide.sh for the IDE}"

if [[ "$ENTRY" != /* ]]; then
    ENTRY="$ROOT/$ENTRY"
fi

[[ -x "$STAGE1/bin/elisac-stage1" ]] || { echo "build: no stage1 product under $STAGE1 (run scripts/doctor.sh for details)" >&2; exit 2; }
[[ -f "$ENTRY" ]] || { echo "build: no entry source at $ENTRY" >&2; exit 2; }
if [[ "$ENTRY" == "$ROOT/src/app/main_native.elisa" ]]; then
    echo "build: src/app/main_native.elisa is the AppKit + Skia IDE entry; use scripts/build_ide.sh" >&2
    exit 2
fi

mkdir -p "$ROOT/build"
bash "$ROOT/scripts/doctor.sh" --sdl --json > "$ROOT/build/provenance-sdl-$NAME.json"

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
    LINK_FLAGS+=(-framework CoreGraphics -framework ImageIO -framework CoreFoundation)
fi
clang "${LINK_FLAGS[@]}" -o "$ROOT/build/$NAME" \
    "${LINK_INPUTS[@]}" \
    -L"$SDL_LIB" -lSDL3 -lSDL3_ttf -Wl,-rpath,"$SDL_LIB"
python3 "$ROOT/scripts/compiler_provenance.py" verify \
    "$ROOT/build/provenance-sdl-$NAME.json" "$STAGE1"
echo "built $ROOT/build/$NAME"
