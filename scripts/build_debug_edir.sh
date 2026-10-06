#!/usr/bin/env bash
# Emit the bounded managed-debug artifact for one saved Elisa source file.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE1_DEFAULT="$ROOT/compiler"
[[ -d "$STAGE1_DEFAULT" ]] || STAGE1_DEFAULT="$ROOT/../Elisa-compiler"
STAGE1="${ELISA_UI_STAGE1:-$STAGE1_DEFAULT}"
ENTRY="$1"
OUTPUT="$2"

if [[ "$ENTRY" != /* ]]; then
    ENTRY="$ROOT/$ENTRY"
fi
if [[ "$OUTPUT" != /* ]]; then
    OUTPUT="$ROOT/$OUTPUT"
fi
[[ -x "$STAGE1/scripts/elisac_stage1.sh" ]] || { echo "debug build: no current Elisa stage1 toolchain at $STAGE1" >&2; exit 2; }
[[ -f "$ENTRY" && "$ENTRY" == *.elisa ]] || { echo "debug build: expected a saved .elisa source file" >&2; exit 2; }
ENTRY_DIRECTORY="$(cd -- "$(dirname -- "$ENTRY")" && pwd)"
ENTRY="$ENTRY_DIRECTORY/$(basename -- "$ENTRY")"
mkdir -p "$(dirname -- "$OUTPUT")"
SOURCE_ROOT="$ENTRY_DIRECTORY"

# Keep debug builds on the current stage1 product and optimizer. EDIR remains a
# bounded managed target, so the compiler or adapter reports unsupported source
# constructs instead of silently falling back to a native, un-debuggable build.
ELISA_EDIR_SOURCE_ROOT="$SOURCE_ROOT" \
    bash "$STAGE1/scripts/elisac_stage1.sh" -O2 -emit edir -o "$OUTPUT" "$ENTRY"
echo "emitted optimized managed-debug artifact $OUTPUT"
