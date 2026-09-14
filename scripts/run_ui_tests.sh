#!/usr/bin/env bash
# Build and run native SDL tests. These require a compiler/framework pair that
# passes the sibling elisa-ui native example build.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Compiles the emitted settings form in an actual framework application unit.
bash scripts/build_native.sh test/codegen/generated_settings_compile.elisa generated_settings_compile >/dev/null
SDL_VIDEODRIVER=dummy ./build/generated_settings_compile

bash scripts/build_core.sh worker/core/document_host.elisa document_host >/dev/null
bash scripts/build_native.sh test/ui/host_client_test.elisa host_client_test >/dev/null
./build/host_client_test

# The shell integration exercises the real isolated preview worker and
# validates the document snapshot through its AppKit render output.
bash scripts/build_preview_worker.sh >/dev/null
bash scripts/build_native.sh test/ui/designer_shell_integration_test.elisa designer_shell_integration_test >/dev/null
./build/designer_shell_integration_test

bash scripts/build_shell_frame_test.sh >/dev/null
bash scripts/smoke_shell_frame.sh

bash scripts/build_native.sh test/ui/designer_shell_test.elisa designer_shell_test >/dev/null
SDL_VIDEODRIVER=dummy ./build/designer_shell_test

bash scripts/build_native.sh src/app/main_native.elisa elisa_ide >/dev/null
bash scripts/smoke_native.sh elisa_ide
echo "all native/UI tests passed"
