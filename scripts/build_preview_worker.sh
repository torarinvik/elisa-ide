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
STAGE1_DEFAULT="$ROOT/compiler"
[[ -d "$STAGE1_DEFAULT" ]] || STAGE1_DEFAULT="$ROOT/../Elisa-compiler"
STAGE1="${ELISA_UI_STAGE1:-$STAGE1_DEFAULT}"
WORKER_OPT_LEVEL="${ELISA_PREVIEW_WORKER_OPT_LEVEL:--O2}"
RUNTIME="$STAGE1/build/runtime/elisacore_runtime.o"
ENTRY="$ROOT/worker/preview/preview_worker_main.elisa"

[[ "$(uname -s)" == "Darwin" ]] || { echo "preview worker: the AppKit canvas backend is macOS only" >&2; exit 2; }
[[ -x "$STAGE1/bin/elisac-stage1" ]] || { echo "preview worker: no stage1 product under $STAGE1" >&2; exit 2; }
[[ -f "$RUNTIME" ]] || { echo "preview worker: no runtime object at $RUNTIME" >&2; exit 2; }
[[ -f "$ENTRY" ]] || { echo "preview worker: no entry source at $ENTRY" >&2; exit 2; }
case "$WORKER_OPT_LEVEL" in
    -O0|-O1|-O2|-O3) ;;
    *) echo "preview worker: ELISA_PREVIEW_WORKER_OPT_LEVEL must be -O0, -O1, -O2, or -O3 (got $WORKER_OPT_LEVEL)" >&2; exit 2 ;;
esac

mkdir -p "$ROOT/build"
bash "$ROOT/scripts/doctor.sh" --worker --json > "$ROOT/build/provenance-preview-worker.json"
python3 - "$ROOT/build/provenance-preview-worker.json" "$WORKER_OPT_LEVEL" <<'PY'
import json
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
optimization_level = sys.argv[2]
with path.open(encoding="utf-8") as source:
    provenance = json.load(source)
facts = provenance.get("facts")
if not isinstance(facts, list):
    raise SystemExit("preview worker: compiler provenance has no facts array")
facts = [fact for fact in facts if not fact.startswith("elisa_preview_worker_opt_level=")]
facts.append(f"elisa_preview_worker_opt_level={optimization_level}")
provenance["facts"] = facts
temporary = path.with_suffix(path.suffix + ".tmp")
temporary.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
os.replace(temporary, path)
PY

APPKIT_SHIM="$UI_ROOT/src/platform/appkit/appkit_canvas_shim.m"
[[ -f "$APPKIT_SHIM" ]] || { echo "preview worker: framework shim missing at $APPKIT_SHIM" >&2; exit 2; }
clang -c -fobjc-arc -Wall -Wextra -Werror -o "$ROOT/build/appkit_canvas_shim.o" "$APPKIT_SHIM"
clang -std=c11 -O2 -c -o "$ROOT/build/designer_posix.o" "$ROOT/src/platform/posix/designer_posix.c"
bash "$STAGE1/scripts/elisac_stage1.sh" "$WORKER_OPT_LEVEL" -o "$ROOT/build/preview_worker.o" "$ENTRY"
clang -Wl,-dead_strip -o "$ROOT/build/preview_worker" \
    "$ROOT/build/preview_worker.o" \
    "$ROOT/build/appkit_canvas_shim.o" \
    "$ROOT/build/designer_posix.o" \
    "$RUNTIME" \
    -framework Cocoa -framework QuartzCore -framework CoreText -framework CoreGraphics -framework ImageIO
python3 "$ROOT/scripts/compiler_provenance.py" verify \
    "$ROOT/build/provenance-preview-worker.json" "$STAGE1"
echo "built $ROOT/build/preview_worker"
