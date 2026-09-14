#!/usr/bin/env bash
# Build the host-free native shell frame regression test.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

exec bash "$ROOT/scripts/build_native.sh" \
    "$ROOT/test/ui/shell_frame_native.elisa" \
    shell_frame_native_test
