#!/usr/bin/env bash
# Build the isolated preview worker (AppKit canvas, macOS).
#
# The worker is a separate executable so a crash or reset inside it cannot
# touch the designer shell's retained tree. It shares the designer's C shim for
# reporting and uses the framework's own AppKit canvas shim for the native
# view; both are compiled here rather than linked from another script's output.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
UI_ROOT="${ELISA_UI_ROOT:-$ROOT/../elisa-ui}"
STAGE1="${ELISA_UI_STAGE1:-$ROOT/../wasm-sdk-compiler}"
RUNTIME="$STAGE1/build/runtime/elisacore_runtime.o"
ENTRY="$ROOT/worker/preview/preview_worker_main.elisa"

[[ "$(uname -s)" == "Darwin" ]] || { echo "preview worker: the AppKit canvas backend is macOS only" >&2; exit 2; }
[[ -x "$STAGE1/bin/elisac-stage1" ]] || { echo "preview worker: no stage1 product under $STAGE1" >&2; exit 2; }
[[ -f "$RUNTIME" ]] || { echo "preview worker: no runtime object at $RUNTIME" >&2; exit 2; }
[[ -f "$ENTRY" ]] || { echo "preview worker: no entry source at $ENTRY" >&2; exit 2; }

mkdir -p "$ROOT/build"
bash "$ROOT/scripts/doctor.sh" --json > "$ROOT/build/provenance-preview-worker.json"

APPKIT_SHIM="$UI_ROOT/src/platform/appkit/appkit_canvas_shim.m"
[[ -f "$APPKIT_SHIM" ]] || { echo "preview worker: framework shim missing at $APPKIT_SHIM" >&2; exit 2; }
clang -c -fobjc-arc -Wall -Wextra -Werror -o "$ROOT/build/appkit_canvas_shim.o" "$APPKIT_SHIM"
clang -std=c11 -O2 -c -o "$ROOT/build/designer_posix.o" "$ROOT/src/platform/posix/designer_posix.c"
bash "$STAGE1/scripts/elisac_stage1.sh" -O0 -o "$ROOT/build/preview_worker.o" "$ENTRY"
clang -Wl,-dead_strip -o "$ROOT/build/preview_worker" \
    "$ROOT/build/preview_worker.o" \
    "$ROOT/build/appkit_canvas_shim.o" \
    "$ROOT/build/designer_posix.o" \
    "$RUNTIME" \
    -framework Cocoa -framework CoreText -framework CoreGraphics -framework ImageIO
echo "built $ROOT/build/preview_worker"
