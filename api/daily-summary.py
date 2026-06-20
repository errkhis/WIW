import importlib.util
import os
from pathlib import Path


_SOURCE = Path(__file__).with_name("daily_summary.py")
_SPEC = importlib.util.spec_from_file_location("daily_summary_impl", _SOURCE)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("daily_summary_impl_not_found")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

handler = _MODULE.handler
