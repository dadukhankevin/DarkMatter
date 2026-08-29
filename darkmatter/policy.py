"""Optional project policy hooks for fetching, hints, and settlement trust."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def load_policy(root: str | Path):
    path = Path(root) / ".darkmatter" / "policy.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("darkmatter_user_policy", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod
