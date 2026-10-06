#!/usr/bin/env bash
# Build and run native SDL tests. The desktop IDE is built separately with
# scripts/build_ide.sh and its pinned AppKit + Skia profile.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Regenerates the settings app, checks user-handler preservation, compiles and
# runs the standalone output, then clicks the generated Save button through
# Elisa-ui's normal event route in a deterministic harness.
bash scripts/smoke_generated_settings.sh
bash scripts/smoke_counter_generation.sh
bash scripts/smoke_primitives_generation.sh
bash scripts/test_external_editor_launch.sh
bash scripts/test_png_decode_orientation.sh
bash scripts/test_host_ipc_paths.sh
bash scripts/test_ide_termination_observer.sh

bash scripts/build_core.sh worker/core/document_host.elisa document_host >/dev/null
bash scripts/build_native.sh test/ui/shell_inspector_test.elisa shell_inspector_test >/dev/null
./build/shell_inspector_test
bash scripts/build_native.sh test/ui/host_client_test.elisa host_client_test >/dev/null
./build/host_client_test
bash scripts/build_native.sh test/ui/profile_shell_guard_test.elisa profile_shell_guard_test >/dev/null
./build/profile_shell_guard_test

# The shell integration exercises the real isolated preview worker and
# validates the document snapshot through its AppKit render output.
bash scripts/build_preview_worker.sh >/dev/null
bash scripts/build_profile_worker.sh >/dev/null
bash scripts/build_package_worker.sh >/dev/null
bash scripts/build_native.sh test/ui/designer_shell_integration_test.elisa designer_shell_integration_test >/dev/null
ELISA_IDE_PACKAGE_ONLY=1 ./build/designer_shell_integration_test
./build/designer_shell_integration_test

bash scripts/build_shell_frame_test.sh >/dev/null
bash scripts/smoke_shell_frame.sh

bash scripts/build_native.sh test/ui/designer_shell_test.elisa designer_shell_test >/dev/null
SDL_VIDEODRIVER=dummy ./build/designer_shell_test

echo "all native/UI tests passed"
