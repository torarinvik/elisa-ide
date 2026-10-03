#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

bash scripts/build_core.sh src/cli/elisa_ide_cli_main.elisa elisa_ide_cli >/dev/null
FIXTURE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/elisa-ide-primitives.XXXXXX")"
trap 'rm -rf "$FIXTURE_DIR"' EXIT
mkdir -p "$FIXTURE_DIR/forms"
cp test/fixtures/primitives-form.elisaform.json "$FIXTURE_DIR/forms/primitives-form.elisaform.json"
cp test/fixtures/primitives-project.elisaproject.json "$FIXTURE_DIR/primitives.elisaproject.json"
python3 scripts/generate_project.py \
  "$FIXTURE_DIR/primitives.elisaproject.json" \
  --cli "$ROOT/build/elisa_ide_cli" \
  --framework-root "$ROOT/framework" >/dev/null

VIEW="$FIXTURE_DIR/generated/PrimitivesExample_view.elisa"
for constructor in check_box radio_button slider_labeled progress_bar_labeled scroll; do
    grep -q "UiHandles::$constructor" "$VIEW"
done

bash scripts/build_project.sh "$FIXTURE_DIR/primitives.elisaproject.json" --name primitives_project_app >/dev/null
SDL_VIDEODRIVER=dummy ./build/primitives_project_app
echo "primitive generation: checkbox, radio, slider, progress, and scroll compiled and ran"
