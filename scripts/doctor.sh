#!/usr/bin/env bash
# Resolve and verify the Elisa IDE's dependencies.
#
# Read-only. Prints the resolved framework/compiler/runtime provenance and
# actionable errors for anything required by the build. Exits 0 when the native
# build inputs are usable, 2 when a required dependency is missing or invalid.
#
# Usage:
#   scripts/doctor.sh          # human-readable report
#   scripts/doctor.sh --json   # machine-readable report for build evidence
set -euo pipefail

JSON=0
[[ "${1:-}" == "--json" ]] && JSON=1

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

UI_ROOT_INPUT="${ELISA_UI_ROOT:-$ROOT/../elisa-ui}"
STAGE1_INPUT="${ELISA_UI_STAGE1:-$ROOT/../wasm-sdk-compiler}"
SDL_LIB="${ELISA_UI_SDL_LIB:-/opt/homebrew/lib}"

issues=()
facts=()
warnings=()

add_fact() { facts+=("$1"); }
add_issue() { issues+=("$1"); }
add_warning() { warnings+=("$1"); }

require_dir() {
    local label="$1" path="$2"
    if [[ -d "$path" ]]; then
        add_fact "$label=$path"
    else
        add_issue "$label: missing directory $path (set the matching environment override or check out the sibling repository)"
    fi
}

# --- Framework -----------------------------------------------------------------

UI_ROOT=""
if [[ -d "$UI_ROOT_INPUT" ]]; then
    UI_ROOT="$(cd -- "$UI_ROOT_INPUT" && pwd)"
    add_fact "framework_root=$UI_ROOT"
    for required in \
        "README.md" \
        "src/core/ui_build.elisa" \
        "src/widgets/ui_handles_build.elisa" \
        "src/platform/sdl3/ui_sdl3_flat.elisa" \
        "scripts/check_toolchain.sh"; do
        if [[ -f "$UI_ROOT/$required" ]]; then
            add_fact "framework_file:$required=present"
        else
            add_issue "framework: $UI_ROOT/$required is missing; the elisa-ui checkout is incomplete or older than this designer expects"
        fi
    done
    if git -C "$UI_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        add_fact "framework_revision=$(git -C "$UI_ROOT" rev-parse HEAD)"
        if [[ -n "$(git -C "$UI_ROOT" status --porcelain --untracked-files=all)" ]]; then
            # A dirty sibling checkout is a reproducibility warning, not a
            # build blocker: a developer intentionally extending the framework
            # while the designer consumes it is the normal case (ADR-001's
            # modifier query landed that way). Missing files and the framework
            # symlink still hard-fail below.
            add_warning "framework: checkout is dirty: $UI_ROOT (build evidence will not be reproducible)"
        fi
    else
        add_issue "framework: checkout has no git provenance: $UI_ROOT"
    fi
else
    add_issue "framework_root: missing directory ${UI_ROOT_INPUT} (set ELISA_UI_ROOT or check out ../elisa-ui)"
fi

LINK_TARGET=""
if [[ -L "$ROOT/framework" ]]; then
    LINK_TARGET="$(readlink "$ROOT/framework")"
    if [[ -n "$UI_ROOT" && "$(cd -P "$ROOT/framework" 2>/dev/null && pwd)" == "$UI_ROOT" ]]; then
        add_fact "framework_symlink=$LINK_TARGET"
    else
        add_issue "framework_symlink: $ROOT/framework -> $LINK_TARGET does not resolve to the configured framework root ($UI_ROOT); source includes will not compile"
    fi
else
    add_issue "framework_symlink: $ROOT/framework is missing or not a symlink; create it with: ln -sfn ../elisa-ui framework"
fi

# --- Toolchain -----------------------------------------------------------------

STAGE1=""
PRODUCT=""
RUNTIME=""
if [[ -d "$STAGE1_INPUT" ]]; then
    STAGE1="$(cd -- "$STAGE1_INPUT" && pwd)"
    add_fact "stage1_root=$STAGE1"
    PRODUCT="$STAGE1/bin/elisac-stage1"
    RUNTIME="$STAGE1/build/runtime/elisacore_runtime.o"
    if [[ -x "$PRODUCT" ]]; then
        add_fact "stage1_product=$PRODUCT"
        add_fact "stage1_product_sha256=$(shasum -a 256 "$PRODUCT" | awk '{print $1}')"
    else
        add_issue "toolchain: no stage1 product at $PRODUCT (run scripts/elisac_stage1.sh --seed in the compiler checkout)"
    fi
    if [[ -f "$RUNTIME" ]]; then
        add_fact "stage1_runtime=$RUNTIME"
        add_fact "stage1_runtime_sha256=$(shasum -a 256 "$RUNTIME" | awk '{print $1}')"
    else
        add_issue "toolchain: no runtime object at $RUNTIME (run scripts/build_runtime_object.sh in the compiler checkout)"
    fi
    if git -C "$STAGE1" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        add_fact "stage1_revision=$(git -C "$STAGE1" rev-parse HEAD)"
        add_fact "stage1_branch=$(git -C "$STAGE1" branch --show-current || echo detached)"
    else
        add_fact "stage1_revision=unverified"
    fi
    # Reuse the framework's own staleness gate in report mode when available.
    if [[ -f "$UI_ROOT/scripts/check_toolchain.sh" && -n "$STAGE1" ]]; then
        stale_report="$(ELISA_UI_STAGE1="$STAGE1" bash "$UI_ROOT/scripts/check_toolchain.sh" --report 2>&1)" || stale_report=""
        if [[ -n "$stale_report" ]]; then
            if [[ "$stale_report" == *"WARNING"* ]]; then
                add_fact "toolchain_stale_check=warning"
                while IFS= read -r line; do
                    [[ "$line" == *"WARNING"* ]] && add_warning "$line"
                done <<< "$stale_report"
            else
                add_fact "toolchain_stale_check=pass"
            fi
        else
            add_issue "toolchain: framework staleness check reported a problem (run ELISA_UI_STAGE1=$STAGE1 bash $UI_ROOT/scripts/check_toolchain.sh --report)"
        fi
    fi
else
    add_issue "stage1_root: missing directory ${STAGE1_INPUT} (set ELISA_UI_STAGE1 or check out ../wasm-sdk-compiler)"
fi

# --- Native build dependencies -------------------------------------------------

if [[ -n "${ELISA_UI_SDL_LIB:-}" || -d "$SDL_LIB" ]]; then
    add_fact "sdl_lib=$SDL_LIB"
else
    add_issue "sdl: library directory $SDL_LIB does not exist (install sdl3 and sdl3_ttf, or set ELISA_UI_SDL_LIB)"
fi
for lib in libSDL3.dylib libSDL3_ttf.dylib; do
    if [[ -e "$SDL_LIB/$lib" ]]; then
        add_fact "sdl_file:$lib=present"
    else
        add_issue "sdl: missing $SDL_LIB/$lib (brew install sdl3 sdl3_ttf, or set ELISA_UI_SDL_LIB)"
    fi
done

if command -v clang >/dev/null 2>&1; then
    add_fact "clang=$(command -v clang)"
else
    add_issue "clang: not found on PATH (install the Xcode command line tools)"
fi
if command -v python3 >/dev/null 2>&1; then
    add_fact "python3=$(command -v python3)"
else
    add_issue "python3: not found on PATH (the compiler's include-expansion step needs it)"
fi

# --- Report --------------------------------------------------------------------

status="ok"
[[ ${#issues[@]} -gt 0 ]] && status="failed"

if [[ "$JSON" == 1 ]]; then
    esc() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }
    printf '{\n'
    printf '  "tool": "elisa-ide-doctor",\n'
    printf '  "status": "%s",\n' "$status"
    printf '  "facts": [\n'
    for i in ${facts[@]+"${!facts[@]}"}; do
        printf '    "%s"' "$(esc "${facts[$i]}")"
        [[ "$i" -lt $((${#facts[@]} - 1)) ]] && printf ','
        printf '\n'
    done
    printf '  ],\n'
    printf '  "warnings": [\n'
    for i in ${warnings[@]+"${!warnings[@]}"}; do
        printf '    "%s"' "$(esc "${warnings[$i]}")"
        [[ "$i" -lt $((${#warnings[@]} - 1)) ]] && printf ','
        printf '\n'
    done
    printf '  ]\n'
    printf '}\n'
else
    for fact in ${facts[@]+"${facts[@]}"}; do
        printf 'doctor: %s\n' "$fact"
    done
    for warning in ${warnings[@]+"${warnings[@]}"}; do
        printf 'doctor: WARNING %s\n' "$warning" >&2
    done
    for issue in ${issues[@]+"${issues[@]}"}; do
        printf 'doctor: ERROR %s\n' "$issue" >&2
    done
    if [[ "$status" == "ok" ]]; then
        printf 'doctor: all required dependencies resolved\n'
    else
        printf 'doctor: %d problem(s) must be resolved before building\n' "${#issues[@]}" >&2
    fi
fi

[[ "$status" == "ok" ]] || exit 2
