#!/usr/bin/env bash
# Stage the local package test helper and its bounded tool resolver.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_ROOT="${1:-$ROOT/build}"
LIB="$OUTPUT_ROOT/package_lib"

mkdir -p "$LIB/src/toolchain"
cp "$ROOT/worker/package/package_runner.py" "$OUTPUT_ROOT/package_runner"
chmod 755 "$OUTPUT_ROOT/package_runner"
cp "$ROOT/src/toolchain/__init__.py" "$ROOT/src/toolchain/resolver.py" "$LIB/src/toolchain/"
printf 'built %s\n' "$OUTPUT_ROOT/package_runner"
