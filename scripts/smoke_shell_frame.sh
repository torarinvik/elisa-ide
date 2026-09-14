#!/usr/bin/env bash
# Run the bounded native shell frame regression test with SDL's dummy driver.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PRODUCT="$ROOT/build/shell_frame_native_test"

[[ -x "$PRODUCT" ]] || {
    echo "shell frame smoke: no product at $PRODUCT (run scripts/build_shell_frame_test.sh)" >&2
    exit 2
}

SDL_VIDEODRIVER=dummy "$PRODUCT"
echo "shell frame smoke: passed"
