#!/usr/bin/env bash
# Run every M00 spike gate in dependency order.
#
# Each spike is a small program with a distinct nonzero exit code per failed
# step. The script builds what it needs and stops at the first failure.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

run_spike() {
    local name="$1" entry="$2"
    bash scripts/build_native.sh "$entry" "$name" >/dev/null
    set +e
    "./build/$name"
    local status=$?
    set -e
    if [[ "$status" -ne 0 ]]; then
        echo "spike $name: FAILED (exit $status)" >&2
        exit 1
    fi
}

run_spike json_lifetime test/spikes/json_lifetime.elisa
run_spike atomic_file_replace test/spikes/atomic_file_replace.elisa
run_spike process_spawn test/spikes/process_spawn.elisa
run_spike widget_capacity test/spikes/widget_capacity.elisa
bash scripts/build_preview_worker.sh >/dev/null
run_spike preview_isolation test/spikes/preview_isolation.elisa

echo "all M00 spikes passed"
