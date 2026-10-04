#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/elisa-ide-termination-test.XXXXXX")"
cleanup() {
    python3 -c 'import shutil, sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$TMP_DIR"
}
trap cleanup EXIT

CC="${ELISA_UI_CC:-clang}"
"$CC" -fobjc-arc -Wall -Wextra -Werror \
    -o "$TMP_DIR/ide_termination_observer_test" \
    "$ROOT/src/platform/appkit/ide_termination_observer.m" \
    "$ROOT/test/source/ide_termination_observer_test.m" \
    -framework Cocoa
"$TMP_DIR/ide_termination_observer_test"
