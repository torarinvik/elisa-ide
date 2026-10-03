#!/usr/bin/env bash
# Exercise project-level staged generation and compile/run the emitted app.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/elisa-ide-project.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT
PROJECT_ROOT="$TMP_DIR/project"
mkdir -p "$PROJECT_ROOT/forms" "$PROJECT_ROOT/src"
cp test/fixtures/settings-project.elisaproject.json "$PROJECT_ROOT/project.elisaproject.json"
cp test/fixtures/settings-form.elisaform.json "$PROJECT_ROOT/forms/settings.elisaform.json"
cp test/codegen/fixtures/settings_handlers.elisa "$PROJECT_ROOT/src/settings_handlers.elisa"
cp "$PROJECT_ROOT/forms/settings.elisaform.json" "$PROJECT_ROOT/forms/secondary.elisaform.json"
python3 - "$PROJECT_ROOT/project.elisaproject.json" "$PROJECT_ROOT/forms/secondary.elisaform.json" <<'PY'
import json, pathlib, sys
project_path, form_path = map(pathlib.Path, sys.argv[1:])
form = json.loads(form_path.read_text(encoding='utf-8'))
form['id'] = 'form-secondary'
form['name'] = 'Secondary'
form['moduleSymbol'] = 'secondary_view'
form_path.write_text(json.dumps(form, ensure_ascii=False), encoding='utf-8')
project = json.loads(project_path.read_text(encoding='utf-8'))
project['forms'].append({'id': 'form-secondary', 'path': 'forms/secondary.elisaform.json'})
project_path.write_text(json.dumps(project, ensure_ascii=False), encoding='utf-8')
PY

# The generated app includes the resolved framework file explicitly. This keeps
# a project portable while allowing this smoke to compile outside the repo.
python3 scripts/generate_project.py \
  "$PROJECT_ROOT/project.elisaproject.json" \
  --cli "$ROOT/build/elisa_ide_cli" \
  --handlers "$PROJECT_ROOT/src/settings_handlers.elisa" \
  --framework-include "$ROOT/framework/src/platform/sdl3/ui_sdl3_flat.elisa" \
  --max-frames 1

GEN="$PROJECT_ROOT/generated"
[[ -s "$GEN/SettingsExample_view.elisa" ]] || { echo "missing generated view" >&2; exit 1; }
[[ -s "$GEN/SettingsExample_view.elisa.map.json" ]] || { echo "missing generated source map" >&2; exit 1; }
[[ -s "$GEN/SettingsExample_secondary_view_view.elisa" ]] || { echo "missing generated secondary view" >&2; exit 1; }
[[ -s "$GEN/SettingsExample_app.elisa" ]] || { echo "missing generated app" >&2; exit 1; }
[[ -s "$GEN/README.md" ]] || { echo "missing generated project README" >&2; exit 1; }
[[ -s "$GEN/.elisa-ide-generation.json" ]] || { echo "missing ownership manifest" >&2; exit 1; }
python3 - "$GEN" <<'PY'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
readme = (root / 'README.md').read_text(encoding='utf-8')
assert 'scripts/build_project.sh project.elisaproject.json' in readme
assert 'Handwritten files are outside that manifest' in readme
assert 'src/settings_handlers.elisa' in readme
manifest = json.loads((root / '.elisa-ide-generation.json').read_text(encoding='utf-8'))
assert manifest['format'] == 'elisa-ide-generation-manifest'
assert manifest['version'] == 1
assert manifest['targetId'] == 'sdl3' and manifest['frameworkId'] == 'elisa-ui'
for item in manifest['files']:
    data = (root / item['path']).read_bytes()
    value = 14695981039346656037
    for byte in data:
        value = ((value ^ byte) * 1099511628211) & 0xffffffffffffffff
    assert item['contentHash'] == f'{value:016x}'
PY

# A second generation is deterministic and leaves the user-owned handler byte
# identical. A manual edit to a generated file is rejected before publication.
handler_before="$(shasum -a 256 "$PROJECT_ROOT/src/settings_handlers.elisa" | awk '{print $1}')"
view_before="$(shasum -a 256 "$GEN/SettingsExample_view.elisa" | awk '{print $1}')"
app_before="$(shasum -a 256 "$GEN/SettingsExample_app.elisa" | awk '{print $1}')"
secondary_before="$(shasum -a 256 "$GEN/SettingsExample_secondary_view_view.elisa" | awk '{print $1}')"
readme_before="$(shasum -a 256 "$GEN/README.md" | awk '{print $1}')"
cp "$GEN/SettingsExample_view.elisa" "$TMP_DIR/view.before.elisa"
python3 scripts/generate_project.py \
  "$PROJECT_ROOT/project.elisaproject.json" \
  --cli "$ROOT/build/elisa_ide_cli" \
  --handlers "$PROJECT_ROOT/src/settings_handlers.elisa" \
  --framework-include "$ROOT/framework/src/platform/sdl3/ui_sdl3_flat.elisa" \
  --max-frames 1 >/dev/null
handler_after="$(shasum -a 256 "$PROJECT_ROOT/src/settings_handlers.elisa" | awk '{print $1}')"
[[ "$handler_before" == "$handler_after" ]] || { echo "handler source changed" >&2; exit 1; }
[[ "$view_before" == "$(shasum -a 256 "$GEN/SettingsExample_view.elisa" | awk '{print $1}')" ]] || { echo "repeat generation changed the view" >&2; exit 1; }
[[ "$app_before" == "$(shasum -a 256 "$GEN/SettingsExample_app.elisa" | awk '{print $1}')" ]] || { echo "repeat generation changed the app" >&2; exit 1; }
[[ "$secondary_before" == "$(shasum -a 256 "$GEN/SettingsExample_secondary_view_view.elisa" | awk '{print $1}')" ]] || { echo "repeat generation changed the secondary view" >&2; exit 1; }
[[ "$readme_before" == "$(shasum -a 256 "$GEN/README.md" | awk '{print $1}')" ]] || { echo "repeat generation changed the project README" >&2; exit 1; }

# Removing a form from the project removes only the previously owned stale
# output; user-owned handlers and the entry view remain intact.
python3 - "$PROJECT_ROOT/project.elisaproject.json" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
project = json.loads(path.read_text(encoding='utf-8'))
project['forms'] = [item for item in project['forms'] if item['id'] != 'form-secondary']
path.write_text(json.dumps(project, ensure_ascii=False), encoding='utf-8')
PY
python3 scripts/generate_project.py \
  "$PROJECT_ROOT/project.elisaproject.json" \
  --cli "$ROOT/build/elisa_ide_cli" \
  --handlers "$PROJECT_ROOT/src/settings_handlers.elisa" \
  --framework-include "$ROOT/framework/src/platform/sdl3/ui_sdl3_flat.elisa" \
  --max-frames 1 >/dev/null
[[ ! -e "$GEN/SettingsExample_secondary_view_view.elisa" ]] || { echo "obsolete generated view survived removal" >&2; exit 1; }
printf '\nmanual edit\n' >> "$GEN/SettingsExample_view.elisa"
if python3 scripts/generate_project.py \
  "$PROJECT_ROOT/project.elisaproject.json" \
  --cli "$ROOT/build/elisa_ide_cli" \
  --handlers "$PROJECT_ROOT/src/settings_handlers.elisa" \
  --framework-include "$ROOT/framework/src/platform/sdl3/ui_sdl3_flat.elisa" \
  --max-frames 1 >/dev/null 2>&1; then
  echo "modified generated output was unexpectedly replaced" >&2
  exit 1
fi
cp "$TMP_DIR/view.before.elisa" "$GEN/SettingsExample_view.elisa"

bash scripts/run_project.sh "$PROJECT_ROOT/project.elisaproject.json" \
  --name generated_project_app \
  --handlers "$PROJECT_ROOT/src/settings_handlers.elisa" \
  --framework-include "$ROOT/framework/src/platform/sdl3/ui_sdl3_flat.elisa" \
  --max-frames 1 >/dev/null

# Relocation is part of the generated-project contract. Move the complete
# project, including its owned output, to a path with spaces and non-ASCII
# characters, then regenerate, compile, and run from the new location. The
# handler remains outside the ownership manifest and all form paths stay
# project-relative.
RELOCATED_ROOT="$TMP_DIR/Relocated Project résumé"
python3 - "$PROJECT_ROOT" "$RELOCATED_ROOT" <<'PY'
import pathlib, shutil, sys
source, target = map(pathlib.Path, sys.argv[1:])
shutil.move(str(source), str(target))
PY
bash scripts/build_project.sh "$RELOCATED_ROOT" \
  --name relocated_project_app \
  --handlers "$RELOCATED_ROOT/src/settings_handlers.elisa" \
  --framework-include "$ROOT/framework/src/platform/sdl3/ui_sdl3_flat.elisa" \
  --max-frames 1 >/dev/null
SDL_VIDEODRIVER=dummy "$ROOT/build/relocated_project_app"
[[ -s "$RELOCATED_ROOT/generated/README.md" ]] || { echo "relocated project lost generated README" >&2; exit 1; }

# A brand-new project has no handwritten handler module yet. Exercise the
# directory form accepted by the IDE Build action, the generated placeholder
# handler shim, and an empty event-dispatch body before any user event exists.
FRESH_ROOT="$TMP_DIR/fresh"
mkdir -p "$FRESH_ROOT/forms"
python3 - "$FRESH_ROOT" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
form = {
    "format": "elisa-ide-form", "schemaVersion": 1, "id": "form-main",
    "name": "Main", "moduleSymbol": "main_view", "root": "node-root",
    "nodes": [{"id": "node-root", "type": "elisa.ui.column", "name": "Main",
               "symbol": "main_column", "properties": {}, "children": [], "events": {}}]
}
project = {
    "format": "elisa-ide-project", "schemaVersion": 1, "projectId": "project-fresh",
    "name": "Fresh Smoke", "applicationId": "org.example.fresh",
    "framework": {"dependency": "elisa-ui"},
    "forms": [{"id": "form-main", "path": "forms/main.elisaform.json"}],
    "entryForm": "form-main", "source": {"roots": ["src"]},
    "generation": {"namespace": "FreshSmoke", "outputRoot": "generated"},
    "targets": [{"id": "desktop", "backend": "appkit-canvas", "renderer": "skia"}]
}
(root / "forms/main.elisaform.json").write_text(json.dumps(form), encoding="utf-8")
(root / "Fresh Smoke.elisaproject.json").write_text(json.dumps(project), encoding="utf-8")
PY
bash scripts/build_project.sh "$FRESH_ROOT" \
  --name fresh_project_smoke >/dev/null
[[ -s "$FRESH_ROOT/generated/README.md" ]] || { echo "fresh project missing generated README" >&2; exit 1; }
grep -q "placeholder handler module" "$FRESH_ROOT/generated/README.md" || { echo "fresh project README omitted handler ownership" >&2; exit 1; }
SDL_VIDEODRIVER=dummy "$ROOT/build/fresh_project_smoke"

echo "project generation: staged output, preserved handlers, rejected edits, and ran"
