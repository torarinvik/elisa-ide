#!/usr/bin/env bash
# Generate, compile, and run a project entry application.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${1:?usage: scripts/run_project.sh <project.elisaproject.json> [generator options]}"
shift
NAME="elisa_project_app"
ARGS=()
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--name" ]]; then NAME="${2:?--name needs a value}"; ARGS+=("$1" "$2"); shift 2; else ARGS+=("$1"); shift; fi
done
if [[ "${#ARGS[@]}" -gt 0 ]]; then
  bash "$ROOT/scripts/build_project.sh" "$PROJECT" --name "$NAME" "${ARGS[@]}"
else
  bash "$ROOT/scripts/build_project.sh" "$PROJECT" --name "$NAME"
fi
SDL_VIDEODRIVER=dummy "$ROOT/build/$NAME"
