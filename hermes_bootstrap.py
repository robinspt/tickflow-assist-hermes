from __future__ import annotations

import sys
from pathlib import Path


def add_local_venv_site_packages(root: Path) -> None:
    home = Path.home()
    candidates = []
    for venv in (root / ".venv", home / ".local" / "share" / "tickflow-assist-hermes" / "venv"):
        candidates.extend(
            [
                venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages",
                venv / "Lib" / "site-packages",
            ]
        )
    for path in candidates:
        if path.exists():
            text = str(path)
            if text not in sys.path:
                sys.path.insert(0, text)
            return
