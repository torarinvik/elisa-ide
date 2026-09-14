#!/usr/bin/env bash
# Run the complete test suite. Core-only tests and native/UI tests are split so
# the headless architecture remains verifiable when a desktop backend is not
# available or its compiler compatibility gate is failing.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

bash scripts/run_core_tests.sh
bash scripts/run_ui_tests.sh
echo "all tests passed"
