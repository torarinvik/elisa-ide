#!/usr/bin/env bash
# Regenerate, compile, and run the first emitted Elisa-ui settings application.
# The settings handler is user-owned; regeneration must leave it byte-identical.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

HANDLER="$ROOT/test/codegen/fixtures/settings_handlers.elisa"
before_hash="$(shasum -a 256 "$HANDLER" | awk '{print $1}')"

ELISA_ALLOW_DIRTY_STAGE1=1 bash scripts/build_core.sh test/codegen/design_codegen_test.elisa design_codegen_test >/dev/null
./build/design_codegen_test

after_hash="$(shasum -a 256 "$HANDLER" | awk '{print $1}')"
[[ "$before_hash" == "$after_hash" ]] || { echo "generated settings: regeneration changed handwritten handlers" >&2; exit 1; }

ELISA_ALLOW_DIRTY_STAGE1=1 bash scripts/build_native.sh test/codegen/generated_settings_compile.elisa generated_settings_compile >/dev/null
SDL_VIDEODRIVER=dummy ./build/generated_settings_compile

ELISA_ALLOW_DIRTY_STAGE1=1 bash scripts/build_native.sh test/ui/generated_settings_runtime_test.elisa generated_settings_runtime_test >/dev/null
./build/generated_settings_runtime_test

echo "generated settings: regeneration preserved handlers; emitted app compiled, ran, and routed a click"
