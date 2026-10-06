#!/usr/bin/env bash
# Generate and compile a project entry application with the resolved compiler.
# The generator owns only its generated directory; source handlers stay user-owned.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${1:?usage: scripts/build_project.sh <project.elisaproject.json> [generator options]}"
shift
NAME="elisa_project_app"
GENERATOR_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="${2:?--name needs a value}"; shift 2 ;;
    *) GENERATOR_ARGS+=("$1"); shift ;;
  esac
done

# Check the complete generated-app toolchain before building the CLI or
# generating project sources. This keeps stale/missing compiler, runtime,
# framework, or SDL inputs from starting a longer build that cannot succeed.
if ! bash "$ROOT/scripts/doctor.sh" --sdl >/dev/null 2>&1; then
  echo "project build stopped: required compiler/runtime or generated-app dependencies are not ready" >&2
  bash "$ROOT/scripts/doctor.sh" --sdl >&2 || true
  exit 2
fi

# The IDE path field intentionally accepts either a manifest or the project
# directory created by New Project. Resolve a directory only when it contains
# one unambiguous manifest; this keeps the process boundary structured while
# making the first Build action usable from a fresh project.
if [[ -d "$PROJECT" ]]; then
  MANIFESTS=()
  while IFS= read -r manifest; do
    MANIFESTS+=("$manifest")
  done < <(find "$PROJECT" -maxdepth 1 -type f -name '*.elisaproject.json' -print | sort)
  if [[ "${#MANIFESTS[@]}" -ne 1 ]]; then
    echo "expected exactly one .elisaproject.json in project directory: $PROJECT" >&2
    exit 2
  fi
  PROJECT="${MANIFESTS[0]}"
fi

if [[ ! -x "$ROOT/build/elisa_ide_cli" ]]; then
  ELISA_ALLOW_DIRTY_STAGE1=1 bash "$ROOT/scripts/build_core.sh" src/cli/elisa_ide_cli_main.elisa elisa_ide_cli >/dev/null
fi
if [[ "${#GENERATOR_ARGS[@]}" -gt 0 ]]; then
  python3 "$ROOT/scripts/generate_project.py" "$PROJECT" --cli "$ROOT/build/elisa_ide_cli" --framework-root "$ROOT/framework" "${GENERATOR_ARGS[@]}"
else
  python3 "$ROOT/scripts/generate_project.py" "$PROJECT" --cli "$ROOT/build/elisa_ide_cli" --framework-root "$ROOT/framework"
fi
PROJECT_ROOT="$(cd -- "$(dirname -- "$PROJECT")" && pwd)"
OUTPUT_RELATIVE="$(python3 - "$PROJECT" <<'PY'
import json, pathlib, sys
project = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
print((project.get('generation') or {}).get('outputRoot', 'generated'))
PY
)"
NAMESPACE="$(python3 - "$PROJECT" <<'PY'
import json, pathlib, re, sys
project = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
value = str((project.get('generation') or {}).get('namespace') or project.get('name') or 'ElisaApp')
value = re.sub(r'[^A-Za-z0-9_]', '_', value)
print(value if value and re.match(r'^[A-Za-z_]', value) else 'ElisaApp')
PY
)"
OUTPUT_ROOT="$PROJECT_ROOT/$OUTPUT_RELATIVE"
APP="$OUTPUT_ROOT/${NAMESPACE}_app.elisa"
[[ -f "$APP" ]] || { echo "generated app not found: $APP" >&2; exit 2; }
ELISA_ALLOW_DIRTY_STAGE1=1 bash "$ROOT/scripts/build_native.sh" "$APP" "$NAME" >/dev/null
printf 'built project application: %s\n' "$ROOT/build/$NAME"
