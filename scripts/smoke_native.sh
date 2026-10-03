#!/usr/bin/env bash
# Headless smoke check for an SDL3 harness or generated-app product.
#
# Usage:
#   scripts/smoke_native.sh <output-name>
#
# Runs the product with the dummy video driver and a bounded frame count so it
# exercises startup, one real frame, and shutdown without opening a window.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="${1:?usage: scripts/smoke_native.sh <SDL output-name>}"
PRODUCT="$ROOT/build/$NAME"

[[ -x "$PRODUCT" ]] || { echo "smoke: no product at $PRODUCT (run scripts/build_native.sh)" >&2; exit 2; }

SDL_VIDEODRIVER=dummy ELISA_UI_SMOKE_FRAMES="${ELISA_UI_SMOKE_FRAMES:-1}" "$PRODUCT"
echo "smoke: $NAME exited cleanly"
