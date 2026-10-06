#!/usr/bin/env bash
# Stage the managed profile runner and its small Python runtime modules.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_ROOT="${1:-$ROOT/build}"
LIB="$OUTPUT_ROOT/profile_lib"

mkdir -p "$LIB/src/profile" "$LIB/src/toolchain"
cp "$ROOT/worker/profile/profile_runner.py" "$OUTPUT_ROOT/profile_runner"
chmod 755 "$OUTPUT_ROOT/profile_runner"
touch "$LIB/src/__init__.py"
cp "$ROOT/src/profile/__init__.py" "$ROOT/src/profile/profile_artifact.py" "$ROOT/src/profile/profile_config.py" "$ROOT/src/profile/profile_history.py" "$ROOT/src/profile/profile_progress.py" "$LIB/src/profile/"
cp "$ROOT/src/toolchain/__init__.py" "$ROOT/src/toolchain/resolver.py" "$LIB/src/toolchain/"
