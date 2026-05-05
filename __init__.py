"""Hermes plugin registration for TickFlow Assist."""

import sys
from pathlib import Path


def _bootstrap() -> None:
    root = Path(__file__).resolve().parent
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    home = Path.home()
    for venv in (root / ".venv", home / ".local" / "share" / "tickflow-assist-hermes" / "venv"):
        candidates = [
            venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages",
            venv / "Lib" / "site-packages",
        ]
        candidates.extend(sorted((venv / "lib").glob("python*/site-packages")))
        for path in candidates:
            if path.exists():
                text = str(path)
                if text not in sys.path:
                    sys.path.insert(0, text)


_bootstrap()

from tickflow_assist.plugin import register

__all__ = ["register"]
