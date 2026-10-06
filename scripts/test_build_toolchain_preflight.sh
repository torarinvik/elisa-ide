#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/elisa-ide-toolchain-preflight.XXXXXX")"
trap 'rm -rf "$TEMP_ROOT"' EXIT

MISSING_STAGE1="$TEMP_ROOT/missing-stage1"
set +e
report="$(ELISA_UI_STAGE1="$MISSING_STAGE1" bash "$ROOT/scripts/build_project.sh" \
  "$TEMP_ROOT/not-reached.elisaproject.json" 2>&1)"
status=$?
set -e

if [[ "$status" -ne 2 ]]; then
  printf 'expected project build to stop with exit 2, got %s\n%s\n' "$status" "$report" >&2
  exit 1
fi
if [[ "$report" != *"project build stopped"* || "$report" != *"missing directory $MISSING_STAGE1"* ]]; then
  printf 'project build did not show the missing-compiler preflight report:\n%s\n' "$report" >&2
  exit 1
fi
if [[ "$report" == *"not-reached.elisaproject.json"* ]]; then
  printf 'project path was processed after the doctor had already failed:\n%s\n' "$report" >&2
  exit 1
fi

printf 'project build toolchain preflight: passed\n'
