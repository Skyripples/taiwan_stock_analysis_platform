"""Compatibility loader for maintenance tools executed by file path."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
_SPEC = importlib.util.spec_from_file_location("_platform_scripts_config", SCRIPTS_DIR / "config.py")
if _SPEC is None or _SPEC.loader is None:
    raise ImportError("Unable to load scripts/config.py")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
for _name in dir(_MODULE):
    if not _name.startswith("_"):
        globals()[_name] = getattr(_MODULE, _name)
