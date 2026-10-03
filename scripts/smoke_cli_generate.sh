#!/usr/bin/env bash
# Exercise the first one-file source-generation CLI workflow end to end.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CLI="$ROOT/build/elisa_ide_cli"
FIXTURE="$ROOT/test/fixtures/settings-form.elisaform.json"
TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/elisa-ide-generate.XXXXXX")"
OUTPUT="$TEMP_DIR/settings_view.elisa"
MAP="$TEMP_DIR/settings_view.elisa.map.json"
BEFORE="$TEMP_DIR/settings_view.before.elisa"
SECOND_LOG="$TEMP_DIR/second-run.log"
SOURCE_LOG="$TEMP_DIR/source-lookup.log"
trap 'rm -rf "$TEMP_DIR"' EXIT

[[ -x "$CLI" ]] || { echo "missing CLI executable: $CLI" >&2; exit 2; }

"$CLI" generate "$FIXTURE" "$OUTPUT" "$MAP"
[[ -s "$MAP" ]] || { echo "generated source map is missing" >&2; exit 1; }
python3 - "$MAP" <<'PY'
import json, sys
value = json.loads(open(sys.argv[1], encoding='utf-8').read())
assert value['format'] == 'elisa-ide-source-map'
assert value['generatedLength'] > 0 and value['spans']
PY
grep -Fq '"Save"' "$OUTPUT" || { echo "generated source is missing the Save caption" >&2; exit 1; }
grep -Fq 'SettingsHandlers::save_clicked(widget, event)' "$OUTPUT" || {
    echo "generated source is missing the Save handler dispatch" >&2
    exit 1
}

# The source command validates the exact generated-byte hash before resolving
# a design origin. Offset 100 is inside the stable form declaration span in
# this fixture and must resolve back to its form ID.
"$CLI" source "$OUTPUT" "$MAP" 100 >"$SOURCE_LOG"
grep -Fq 'origin=' "$SOURCE_LOG" || { cat "$SOURCE_LOG" >&2; echo "source lookup omitted origin" >&2; exit 1; }
grep -Fq 'form-settings' "$SOURCE_LOG" || { cat "$SOURCE_LOG" >&2; echo "source lookup omitted form identity" >&2; exit 1; }

cp "$OUTPUT" "$TEMP_DIR/settings_view.changed.elisa"
printf '\n# changed after generation\n' >> "$TEMP_DIR/settings_view.changed.elisa"
if "$CLI" source "$TEMP_DIR/settings_view.changed.elisa" "$MAP" 100 >"$TEMP_DIR/stale-source.log" 2>&1; then
    echo "source lookup unexpectedly accepted a stale generated file" >&2
    exit 1
fi
grep -Fq 'stale or invalid' "$TEMP_DIR/stale-source.log" || {
    cat "$TEMP_DIR/stale-source.log" >&2
    echo "stale source lookup did not explain rejection" >&2
    exit 1
}

cp "$OUTPUT" "$BEFORE"
if "$CLI" generate "$FIXTURE" "$OUTPUT" "$MAP" >"$SECOND_LOG" 2>&1; then
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
