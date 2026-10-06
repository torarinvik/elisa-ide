#!/usr/bin/env bash
# Build the macOS Elisa IDE with the AppKit window host and Skia canvas.
#
# This is the product build. SDL3 remains in the separate test harnesses and
# any SDL-backed generated-application profile; it is not linked into the IDE.
# The pinned Skia checkout is supplied by the developer through SKIA_ROOT and
# is never downloaded or built implicitly by this script.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
UI_ROOT="${ELISA_UI_ROOT:-$ROOT/../elisa-ui}"
STAGE1_DEFAULT="$ROOT/compiler"
[[ -d "$STAGE1_DEFAULT" ]] || STAGE1_DEFAULT="$ROOT/../Elisa-compiler"
STAGE1="${ELISA_UI_STAGE1:-$STAGE1_DEFAULT}"
# The complete single-unit IDE currently stalls in LLVM's optimization passes
# at O1/O2. Keep the default build launchable; developers can still select an
# optimized level explicitly with ELISA_IDE_OPT_LEVEL once that bottleneck is
# resolved and reverified against the whole product.
IDE_OPT_LEVEL="${ELISA_IDE_OPT_LEVEL:--O0}"
SKIA_ROOT="${SKIA_ROOT:-}"
SKIA_OUT="${SKIA_OUT:-${SKIA_ROOT:+$SKIA_ROOT/out/elisa}}"
ENTRY="$ROOT/src/app/main_native.elisa"
RUNTIME="$STAGE1/build/runtime/elisacore_runtime.o"
IDE_NAME="elisa_ide"
BUILD_DIR="$ROOT/build"
SKIA_BUILD_DIR="$BUILD_DIR/ide-skia"
# Keep the default bundle path stable for normal builds; an alternate output
# lets a new version be fully built and verified before the running app quits.
APP="${ELISA_IDE_APP_OUTPUT:-$BUILD_DIR/Elisa IDE.app}"

fail() {
    printf 'build_ide: %s\n' "$1" >&2
    exit 2
}

[[ "$(uname -s)" == "Darwin" ]] || fail "the AppKit + Skia IDE backend is macOS-only"
[[ -n "$SKIA_ROOT" ]] || fail "set SKIA_ROOT to the clean elisa-ui pinned Skia checkout (revision 9c7b2dffb2433f5a0cc2b77f06025a09126807ed); see $UI_ROOT/third_party/skia.lock"
[[ -n "$SKIA_OUT" ]] || fail "could not resolve SKIA_OUT (default is SKIA_ROOT/out/elisa)"
[[ -x "$STAGE1/bin/elisac-stage1" ]] || fail "no stage1 product at $STAGE1/bin/elisac-stage1 (run scripts/doctor.sh for details)"
case "$IDE_OPT_LEVEL" in
    -O0|-O1|-O2|-O3) ;;
    *) fail "ELISA_IDE_OPT_LEVEL must be -O0, -O1, -O2, or -O3 (got $IDE_OPT_LEVEL)" ;;
esac
[[ -f "$RUNTIME" ]] || fail "no runtime object at $RUNTIME (build the stage1 runtime first)"
[[ -f "$ENTRY" ]] || fail "no IDE entry source at $ENTRY"
[[ -d "$UI_ROOT" ]] || fail "elisa-ui framework checkout is missing at $UI_ROOT (set ELISA_UI_ROOT)"

FRAMEWORK_SKIA_VERIFY="$UI_ROOT/scripts/verify_skia_build.sh"
APPKIT_SHIM="$UI_ROOT/src/platform/appkit/appkit_canvas_shim.m"
APPKIT_SKIA_HOST="$UI_ROOT/src/platform/appkit/appkit_skia_host.cpp"
SKIA_CANVAS_SHIM="$UI_ROOT/src/platform/skia/skia_canvas_shim.cpp"
SKIA_TEXT_SHIM="$UI_ROOT/src/platform/skia/skia_text_shim.cpp"
for required in \
    "$FRAMEWORK_SKIA_VERIFY" \
    "$APPKIT_SHIM" \
    "$APPKIT_SKIA_HOST" \
    "$SKIA_CANVAS_SHIM" \
    "$SKIA_TEXT_SHIM" \
    "$ROOT/src/platform/skia/ide_skia_image_bridge.cpp"; do
    [[ -f "$required" ]] || fail "required source or verifier is missing: $required"
done

[[ -f "$SKIA_ROOT/include/core/SkCanvas.h" ]] || fail "Skia headers are missing at $SKIA_ROOT/include; set SKIA_ROOT to the pinned Skia checkout"
[[ -f "$SKIA_OUT/libskia.a" ]] || fail "Skia archive is missing at $SKIA_OUT/libskia.a; build the pinned checkout with $UI_ROOT/scripts/build_skia.sh"

mkdir -p "$BUILD_DIR" "$SKIA_BUILD_DIR"
bash "$ROOT/scripts/build_profile_worker.sh" "$BUILD_DIR"
bash "$ROOT/scripts/build_package_worker.sh" "$BUILD_DIR"

# The IDE doctor mode checks the framework/compiler/toolchain and the Skia
# source/build provenance together, and leaves a machine-readable record for
# this concrete product build.
ELISA_UI_STAGE1="$STAGE1" SKIA_ROOT="$SKIA_ROOT" SKIA_OUT="$SKIA_OUT" \
    bash "$ROOT/scripts/doctor.sh" --skia --json > "$BUILD_DIR/provenance-ide.json"

python3 - "$BUILD_DIR/provenance-ide.json" "$IDE_OPT_LEVEL" <<'PY'
import json
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
optimization_level = sys.argv[2]
with path.open(encoding="utf-8") as source:
    provenance = json.load(source)
facts = provenance.get("facts")
if not isinstance(facts, list):
    raise SystemExit("build_ide: compiler provenance has no facts array")
facts = [fact for fact in facts if not fact.startswith("elisa_ide_opt_level=")]
facts.append(f"elisa_ide_opt_level={optimization_level}")
provenance["facts"] = facts
temporary = path.with_suffix(path.suffix + ".tmp")
temporary.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
os.replace(temporary, path)
PY

# Keep the framework's source revision, lockfile digest and archive digest as a
# second explicit gate at the point where the archive will be linked.
SKIA_ROOT="$SKIA_ROOT" SKIA_OUT="$SKIA_OUT" \
    bash "$FRAMEWORK_SKIA_VERIFY"
# The sibling compiler is intentionally under active optimization development.
# Its complete Elisa source tree is fingerprinted above and rechecked after the
# build; the compiler wrapper still rejects a product older than any source.
ELISA_ALLOW_DIRTY_STAGE1=1 ELISA_UI_STAGE1="$STAGE1" bash "$UI_ROOT/scripts/check_toolchain.sh"

# These sibling products are part of the IDE's local preview architecture.
# Their helpers record their own provenance and build the AppKit preview
# worker without Skia; the IDE's main process owns the Skia renderer.
bash "$ROOT/scripts/build_core.sh" "$ROOT/worker/core/document_host.elisa" document_host
bash "$ROOT/scripts/build_preview_worker.sh"

# Compile the product's retained Elisa application and the native adapters.
# Use a dedicated object directory so SDL/test or worker builds cannot leave
# incompatible Skia shim object files for this product to consume.
clang -c -fobjc-arc -Wall -Wextra -Werror -DELISA_UI_USE_SKIA \
    -o "$SKIA_BUILD_DIR/appkit_canvas_skia_shim.o" "$APPKIT_SHIM"
clang -c -fobjc-arc -Wall -Wextra -Werror \
    -o "$SKIA_BUILD_DIR/ide_termination_observer.o" \
    "$ROOT/src/platform/appkit/ide_termination_observer.m"
ide_root_define="-DELISA_IDE_SOURCE_ROOT=\"$ROOT\""
clang -std=c11 -Wall -Wextra -Werror "$ide_root_define" \
    -c "$ROOT/src/platform/posix/ide_app_environment.c" \
    -o "$SKIA_BUILD_DIR/ide_app_environment.o"
clang -std=c11 -O2 -c -o "$SKIA_BUILD_DIR/designer_posix.o" \
    "$ROOT/src/platform/posix/designer_posix.c"
bash "$STAGE1/scripts/elisac_stage1.sh" "$IDE_OPT_LEVEL" \
    -o "$SKIA_BUILD_DIR/${IDE_NAME}.o" "$ENTRY"

clang++ -std=c++20 -DSK_BUILD_FOR_MAC -fPIC -I"$UI_ROOT" -I"$SKIA_ROOT" -c \
    "$APPKIT_SKIA_HOST" -o "$SKIA_BUILD_DIR/appkit_skia_host.o"
clang++ -std=c++17 -fPIC -I"$SKIA_ROOT" -c \
    "$SKIA_CANVAS_SHIM" -o "$SKIA_BUILD_DIR/skia_canvas_shim.o"
clang++ -std=c++17 -fPIC -I"$SKIA_ROOT" -c \
    "$SKIA_TEXT_SHIM" -o "$SKIA_BUILD_DIR/skia_text_shim.o"
clang++ -std=c++17 -fPIC -I"$SKIA_ROOT" -c \
    "$ROOT/src/platform/skia/ide_skia_image_bridge.cpp" \
    -o "$SKIA_BUILD_DIR/ide_skia_image_bridge.o"

link_inputs=(
    "$SKIA_BUILD_DIR/${IDE_NAME}.o"
    "$SKIA_BUILD_DIR/appkit_canvas_skia_shim.o"
    "$SKIA_BUILD_DIR/ide_termination_observer.o"
    "$SKIA_BUILD_DIR/appkit_skia_host.o"
    "$SKIA_BUILD_DIR/skia_canvas_shim.o"
    "$SKIA_BUILD_DIR/skia_text_shim.o"
    "$SKIA_BUILD_DIR/ide_skia_image_bridge.o"
    "$SKIA_BUILD_DIR/ide_app_environment.o"
    "$SKIA_BUILD_DIR/designer_posix.o"
    "$RUNTIME"
    "$SKIA_OUT/libskshaper.a"
    "$SKIA_OUT/libskia.a"
)
for extra in "$SKIA_OUT/libpng.a" "$SKIA_OUT/libzlib.a"; do
    [[ -f "$extra" ]] && link_inputs+=("$extra")
done

clang++ -Wl,-dead_strip -o "$BUILD_DIR/$IDE_NAME" "${link_inputs[@]}" \
    -framework Cocoa \
    -framework QuartzCore \
    -framework CoreText \
    -framework CoreGraphics \
    -framework ImageIO \
    -lz

python3 "$ROOT/scripts/compiler_provenance.py" verify \
    "$BUILD_DIR/provenance-ide.json" "$STAGE1"

# Assemble the standard double-clickable macOS application bundle. Stage the
# bundle separately so a failed packaging step cannot leave a half-written
# product at the expected path.
STAGED_APP="$(mktemp -d "$BUILD_DIR/.elisa-ide-app.XXXXXX")/Elisa IDE.app"
cleanup_staged_app() {
    local staged_parent
    staged_parent="$(dirname -- "$STAGED_APP")"
    rm -rf "$staged_parent"
}
trap cleanup_staged_app EXIT
MACOS="$STAGED_APP/Contents/MacOS"
RESOURCES="$STAGED_APP/Contents/Resources"
mkdir -p "$MACOS" "$RESOURCES"
cp "$BUILD_DIR/$IDE_NAME" "$MACOS/$IDE_NAME"
cp "$BUILD_DIR/document_host" "$RESOURCES/document_host"
cp "$BUILD_DIR/preview_worker" "$RESOURCES/preview_worker"
cp "$BUILD_DIR/profile_runner" "$RESOURCES/profile_runner"
cp -R "$BUILD_DIR/profile_lib" "$RESOURCES/profile_lib"
cp "$BUILD_DIR/package_runner" "$RESOURCES/package_runner"
cp -R "$BUILD_DIR/package_lib" "$RESOURCES/package_lib"
codesign --force --sign - "$RESOURCES/document_host" >/dev/null
codesign --force --sign - "$RESOURCES/preview_worker" >/dev/null
codesign --force --sign - "$MACOS/$IDE_NAME" >/dev/null
PLIST="$STAGED_APP/Contents/Info.plist"
plutil -create xml1 "$PLIST"
plutil -insert CFBundleExecutable -string "$IDE_NAME" "$PLIST"
plutil -insert CFBundleIdentifier -string "org.elisa.ide" "$PLIST"
plutil -insert CFBundleName -string "Elisa IDE" "$PLIST"
plutil -insert CFBundleDisplayName -string "Elisa IDE" "$PLIST"
plutil -insert CFBundlePackageType -string "APPL" "$PLIST"
plutil -insert CFBundleShortVersionString -string "0.1.0" "$PLIST"
plutil -insert CFBundleVersion -string "1" "$PLIST"
plutil -insert NSHighResolutionCapable -bool YES "$PLIST"
plutil -insert NSPrincipalClass -string "NSApplication" "$PLIST"
codesign --force --sign - "$STAGED_APP" >/dev/null

rm -rf "$APP"
mv "$STAGED_APP" "$APP"
cleanup_staged_app
trap - EXIT

printf 'built %s\n' "$BUILD_DIR/$IDE_NAME"
printf 'built %s\n' "$APP"
