#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "skip png_decode_rgba32_top_down: ImageIO decoder is macOS-only"
    exit 0
fi

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/elisa-ide-png-orientation.XXXXXX")"
cleanup() {
    python3 -c 'import shutil, sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$TMP_DIR"
}
trap cleanup EXIT

python3 - "$TMP_DIR/orientation.png" <<'PY'
import pathlib
import struct
import sys
import zlib

width = height = 2
rows = [bytes([255, 0, 0, 255] * width), bytes([0, 0, 255, 255] * width)]

def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)

png = b"\x89PNG\r\n\x1a\n"
png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
png += chunk(b"IDAT", zlib.compress(b"".join(b"\x00" + row for row in rows)))
png += chunk(b"IEND", b"")
pathlib.Path(sys.argv[1]).write_bytes(png)
PY

CC="${ELISA_UI_CC:-clang}"
"$CC" -std=c11 -O2 -c -o "$TMP_DIR/designer_posix.o" src/platform/posix/designer_posix.c
"$CC" -Wl,-dead_strip -framework CoreGraphics -framework CoreFoundation -framework ImageIO \
    -o "$TMP_DIR/png_decode_orientation_test" \
    test/source/png_decode_orientation_test.c "$TMP_DIR/designer_posix.o"
"$TMP_DIR/png_decode_orientation_test" "$TMP_DIR/orientation.png"
