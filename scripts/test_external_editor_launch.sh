#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/elisa-ide-editor-test.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT

CC="${ELISA_UI_CC:-clang}"
"$CC" -std=c11 -O2 -c -o "$TMP_DIR/designer_posix.o" src/platform/posix/designer_posix.c
LINK_FLAGS=(-Wl,-dead_strip)
if [[ "$(uname -s)" == "Darwin" ]]; then
    LINK_FLAGS+=(-framework CoreGraphics -framework ImageIO -framework CoreFoundation)
fi
"$CC" "${LINK_FLAGS[@]}" -o "$TMP_DIR/external_editor_launch_test" \
    test/source/external_editor_launch_test.c "$TMP_DIR/designer_posix.o"
"$TMP_DIR/external_editor_launch_test" "$ROOT/test/source/editor_argv_recorder.py"
