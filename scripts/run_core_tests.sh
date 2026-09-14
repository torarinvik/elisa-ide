#!/usr/bin/env bash
# Build and run portable model, schema, command, save, generation, preview-frame,
# and document-host protocol tests.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TESTS=(
    test/model/design_ids_test.elisa
    test/model/design_geometry_test.elisa
    test/model/design_hit_test_test.elisa
    test/model/design_registry_test.elisa
    test/model/design_codec_test.elisa
    test/model/design_validate_test.elisa
    test/model/design_migrate_test.elisa
    test/model/design_command_test.elisa
    test/model/design_save_test.elisa
    test/model/workspace_preferences_test.elisa
    test/model/workspace_preferences_store_test.elisa
    test/model/shell_window_test.elisa
    test/codegen/design_codegen_test.elisa
    test/codegen/generation_manifest_test.elisa
    test/codegen/source_map_test.elisa
    test/codegen/source_map_json_test.elisa
    test/preview/preview_protocol_test.elisa
    test/preview/preview_payloads_test.elisa
)

for entry in "${TESTS[@]}"; do
    name="$(basename "$entry" .elisa)"
    bash scripts/build_core.sh "$entry" "$name" >/dev/null
    set +e
    "./build/$name"
    status=$?
    set -e
    if [[ "$status" -ne 0 ]]; then
        echo "test $name: FAILED (exit $status)" >&2
        exit 1
    fi
done

# CLI smoke: validate shipped fixtures and exercise schema migration.
bash scripts/build_core.sh src/cli/elisa_ide_cli_main.elisa elisa_ide_cli >/dev/null
./build/elisa_ide_cli validate test/fixtures/settings-form.elisaform.json >/dev/null
./build/elisa_ide_cli validate test/fixtures/settings-project.elisaproject.json >/dev/null
./build/elisa_ide_cli validate test/fixtures/unknown-component-form.elisaform.json >/dev/null
./build/elisa_ide_cli migrate test/fixtures/legacy-form-v0.elisaform.json build/spikes/legacy-migrated.json >/dev/null
scripts/smoke_cli_generate.sh

# The core document process and its cross-process protocol are UI-independent.
bash scripts/build_core.sh worker/core/document_host.elisa document_host >/dev/null
python3 test/preview/host_protocol_test.py
echo "all core tests passed"
