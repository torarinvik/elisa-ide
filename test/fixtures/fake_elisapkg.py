#!/usr/bin/env python3
"""Record a harmless invocation for the native shell package-task test."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time


log = Path(os.environ["FAKE_ELISAPKG_LOG"])
with log.open("a", encoding="utf-8") as stream:
    stream.write(f"cwd={Path.cwd()}\n")
    stream.write("argv=" + " ".join(sys.argv[1:]) + "\n")
started = os.environ.get("FAKE_ELISAPKG_STARTED_FILE", "")
if started:
    Path(started).write_text(f"pid={os.getpid()}\n", encoding="utf-8")
sleep_seconds = float(os.environ.get("FAKE_ELISAPKG_SLEEP", "0") or "0")
if sleep_seconds > 0:
    time.sleep(min(sleep_seconds, 30.0))
exit_code = int(os.environ.get("FAKE_ELISAPKG_EXIT", "0") or "0")
if exit_code:
    print("fake elisapkg test failed")
    print("[ RUN      ] fixture_assertion")
    print(f"[ FAILED   ] fixture_assertion (exit {exit_code})")
    print("[ STDOUT   ] fixture_assertion")
    print("    fixture assertion output for the test explorer")
    print("[ RUN      ] fixture_cleanup")
    print("[       OK ] fixture_cleanup")
    print("[ SUMMARY  ] 2 test(s) selected; passed=1 skipped=0 failed=1")
    raise SystemExit(exit_code)
print("fake elisapkg test succeeded")
print("[ RUN      ] fixture_assertion")
print("[       OK ] fixture_assertion")
print("[ RUN      ] fixture_optional")
print("[ SKIPPED  ] fixture_optional (annotation-requested)")
print("[ SUMMARY  ] 2 test(s) selected; passed=1 skipped=1 failed=0")
