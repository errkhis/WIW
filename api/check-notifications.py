import importlib.util
from pathlib import Path


_SOURCE = Path(__file__).with_name("check_notifications.py")
_SPEC = importlib.util.spec_from_file_location("check_notifications_impl", _SOURCE)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("check_notifications_impl_not_found")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

handler = _MODULE.handler
