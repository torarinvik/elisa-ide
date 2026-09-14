#!/usr/bin/env bash
# Exercise the first one-file source-generation CLI workflow end to end.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CLI="$ROOT/build/elisa_ide_cli"
FIXTURE="$ROOT/test/fixtures/settings-form.elisaform.json"
TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/elisa-ide-generate.XXXXXX")"
OUTPUT="$TEMP_DIR/settings_view.elisa"
BEFORE="$TEMP_DIR/settings_view.before.elisa"
SECOND_LOG="$TEMP_DIR/second-run.log"
trap 'rm -rf "$TEMP_DIR"' EXIT

[[ -x "$CLI" ]] || { echo "missing CLI executable: $CLI" >&2; exit 2; }

"$CLI" generate "$FIXTURE" "$OUTPUT"
grep -Fq '"Save"' "$OUTPUT" || { echo "generated source is missing the Save caption" >&2; exit 1; }
grep -Fq 'SettingsHandlers::save_clicked(widget, event)' "$OUTPUT" || {
    echo "generated source is missing the Save handler dispatch" >&2
    exit 1
}

cp "$OUTPUT" "$BEFORE"
if "$CLI" generate "$FIXTURE" "$OUTPUT" >"$SECOND_LOG" 2>&1; then
    echo "generate unexpectedly replaced an existing output" >&2
    exit 1
fi
grep -Fq 'refusing to replace' "$SECOND_LOG" || {
    cat "$SECOND_LOG" >&2
    echo "second invocation did not explain the existing-output refusal" >&2
    exit 1
}
cmp -s "$BEFORE" "$OUTPUT" || { echo "existing output changed after refusal" >&2; exit 1; }

echo "cli generate smoke: passed"
