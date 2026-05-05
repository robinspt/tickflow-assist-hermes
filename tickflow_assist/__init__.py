"""TickFlow Assist Hermes plugin package."""

import sys
from pathlib import Path


def _bootstrap() -> None:
    root = Path(__file__).resolve().parents[1]
    home = Path.home()
    for venv in (root / ".venv", home / ".local" / "share" / "tickflow-assist-hermes" / "venv"):
        for path in (
            venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages",
            venv / "Lib" / "site-packages",
        ):
            if path.exists():
                text = str(path)
                if text not in sys.path:
                    sys.path.insert(0, text)
                return


_bootstrap()

from .plugin import register

__all__ = ["register"]
