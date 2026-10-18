"""Stack frames for an error, innermost first, with the app's own code marked
(in_app) so LoggerAI groups errors by where they happened in your code."""
from __future__ import annotations

import os
import sysconfig
from typing import List

_LIBS = tuple(p for p in {sysconfig.get_paths().get("stdlib"), sysconfig.get_paths().get("platstdlib")} if p)
_OWN = os.path.dirname(os.path.abspath(__file__))


def _in_app(path: str) -> bool:
    if not path or path.startswith("<"):
        return False
    if "site-packages" in path or "dist-packages" in path or path.startswith(_OWN):
        return False
    return not path.startswith(_LIBS)


def frames(exc: BaseException, root: str, limit: int = 50) -> List[dict]:
    out = []
    tb = exc.__traceback__
    entries = []
    while tb is not None:
        entries.append(tb)
        tb = tb.tb_next
    for tb in reversed(entries[-limit:]):  # innermost first
        code = tb.tb_frame.f_code
        path = os.path.abspath(code.co_filename)
        rel = os.path.relpath(path, root) if path.startswith(root + os.sep) else path
        qual = getattr(code, "co_qualname", code.co_name)  # 3.11+: Class.method
        cls = qual.rsplit(".", 1)[0] if "." in qual and "<locals>" not in qual else None
        out.append({"file": rel, "line": tb.tb_lineno, "function": code.co_name, "class": cls, "in_app": _in_app(path)})
    return out


def type_name(exc: BaseException) -> str:
    t = type(exc)
    mod = t.__module__
    return t.__qualname__ if mod in ("builtins", "__main__") else f"{mod}.{t.__qualname__}"
