#!/usr/bin/env python3
"""Successful no-op viewer for supervised profile report integration tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path


log_path = os.environ.get("ELISA_IDE_TEST_VIEWER_LOG")
if log_path:
    Path(log_path).write_text(sys.argv[1] if len(sys.argv) == 2 else "invalid-argv", encoding="utf-8")
