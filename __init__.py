"""Hermes plugin registration for TickFlow Assist."""

import os
import site
import sys
from pathlib import Path


def _venv_candidates(root: Path) -> list[Path]:
    raw_paths: list[str] = []
    env_path = os.environ.get("TICKFLOW_ASSIST_VENV")
    if env_path:
        raw_paths.append(env_path)
    marker = root / ".tickflow-assist-venv"
    if marker.exists():
        try:
            raw_paths.append(marker.read_text(encoding="utf-8").strip())
        except OSError:
            pass
    home = Path.home()
    raw_paths.extend(
        [
            str(root / ".venv"),
            str(home / ".local" / "share" / "tickflow-assist-hermes" / "venv"),
        ]
    )

    seen: set[str] = set()
    output: list[Path] = []
    for raw in raw_paths:
        if not raw:
            continue
        path = Path(raw).expanduser()
        text = str(path)
        if text not in seen:
            seen.add(text)
            output.append(path)
    return output


def _site_packages(venv: Path) -> list[Path]:
    return [
        venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages",
        venv / "Lib" / "site-packages",
    ]


def _add_path(path: Path) -> None:
    text = str(path)
    try:
        site.addsitedir(text)
    except Exception:
        pass
    while text in sys.path:
        sys.path.remove(text)
    sys.path.insert(0, text)


def _bootstrap() -> None:
    root = Path(__file__).resolve().parent
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    for venv in _venv_candidates(root):
        for path in _site_packages(venv):
            if path.exists():
                _add_path(path)


_bootstrap()

from tickflow_assist.plugin import register

__all__ = ["register"]
