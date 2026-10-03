#!/usr/bin/env python3
"""Tiny executable fixture that records the editor handoff argv verbatim."""

import os
import json
import sys

output = os.environ["ELISA_IDE_EDITOR_TEST_OUTPUT"]
with open(output, "w", encoding="utf-8", newline="") as stream:
    json.dump(sys.argv[1:], stream, ensure_ascii=False)
    stream.write("\n")
