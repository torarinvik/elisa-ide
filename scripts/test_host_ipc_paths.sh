#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/elisa-ide-host-ipc-test.XXXXXX")"
cleanup() {
    python3 -c 'import shutil, sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$TMP_DIR"
}
trap cleanup EXIT

CC="${ELISA_UI_CC:-clang}"
"$CC" -std=c11 -O2 -Wall -Wextra -Werror -c \
    -o "$TMP_DIR/designer_posix.o" src/platform/posix/designer_posix.c
if [[ "$(uname -s)" == "Darwin" ]]; then
    "$CC" -Wl,-dead_strip -framework CoreGraphics -framework CoreFoundation -framework ImageIO \
        -o "$TMP_DIR/host_ipc_paths_test" \
        test/source/host_ipc_paths_test.c "$TMP_DIR/designer_posix.o"
else
    "$CC" -o "$TMP_DIR/host_ipc_paths_test" \
        test/source/host_ipc_paths_test.c "$TMP_DIR/designer_posix.o"
fi
"$TMP_DIR/host_ipc_paths_test"
