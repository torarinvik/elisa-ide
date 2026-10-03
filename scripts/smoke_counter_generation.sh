#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

bash scripts/build_core.sh src/cli/elisa_ide_cli_main.elisa elisa_ide_cli >/dev/null
python3 - <<'PY'
from pathlib import Path
for name in ("build/generated_counter_view.elisa", "build/generated_counter_view.elisa.map.json"):
    Path(name).unlink(missing_ok=True)
PY
./build/elisa_ide_cli generate test/fixtures/counter-form.elisaform.json build/generated_counter_view.elisa build/generated_counter_view.elisa.map.json >/dev/null
bash scripts/build_native.sh test/ui/generated_counter_runtime_test.elisa generated_counter_runtime_test >/dev/null
./build/generated_counter_runtime_test
echo "counter generation: emitted app routed two clicks and updated visible state"
