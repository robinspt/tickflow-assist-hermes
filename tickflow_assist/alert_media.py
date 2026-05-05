from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Iterable

from .utils import now_text


@dataclass(frozen=True)
class AlertCardInput:
    title: str
    label: str
    name: str
    symbol: str
    current_price: float
    trigger_price: float
    note: str
    points: list[tuple[str, float]]
    levels: dict[str, float | None]


def write_alert_card(base_dir: str, card: AlertCardInput) -> Path:
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise RuntimeError("Pillow is required to render PNG alert cards") from exc

    cleanup_alert_media(base_dir)
    output_dir = Path(base_dir).expanduser().resolve() / "alert-media" / "tmp" / now_text()[:10]
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = "_".join([_safe(now_text().replace(":", "-").replace(" ", "-")), _safe(card.symbol), _safe(card.label)]) + ".png"
    path = output_dir / filename

    width, height = 960, 640
    image = Image.new("RGB", (width, height), "#07111f")
    draw = ImageDraw.Draw(image)
    _draw_vertical_gradient(draw, width, height, "#07111f", "#172b47")

    accent = "#52d6ff"
    if card.label in {"突破", "止盈"}:
        accent = "#65e6a8"
    if card.label == "止损":
        accent = "#ff7676"

    draw.rounded_rectangle((28, 28, 932, 612), radius=26, fill="#0d1d31", outline="#274766", width=2)
    draw.rounded_rectangle((28, 28, 932, 44), radius=8, fill=accent)
    draw.text((54, 62), "TICKFLOW ALERT", fill="#90a8c4", font=_font(22, bold=True))
    draw.text((54, 104), f"{card.name}  {card.symbol}", fill="#f4f8ff", font=_font(38, bold=True))
    draw.text((54, 154), f"{card.title} | {now_text()}", fill="#a8bdd6", font=_font(21))

    draw.rounded_rectangle((720, 70, 892, 116), radius=23, fill="#122f43", outline=accent, width=2)
    draw.text((806, 93), card.label, anchor="mm", fill="#f6fbff", font=_font(26, bold=True))
    draw.text((720, 156), "当前价", fill="#8fa9c5", font=_font(20))
    draw.text((720, 202), f"{card.current_price:.2f}", fill="#f7fbff", font=_font(44, bold=True))
    draw.text((720, 236), f"触发位 {card.trigger_price:.2f}", fill="#aec3da", font=_font(20))

    chart = (54, 260, 686, 470)
    draw.rounded_rectangle(chart, radius=18, fill="#0a1728", outline="#263f5e", width=1)
    _draw_chart(draw, chart, card.points, card.current_price, card.trigger_price, accent)

    levels_box = (710, 278, 900, 470)
    draw.rounded_rectangle(levels_box, radius=18, fill="#0a1728", outline="#263f5e", width=1)
    draw.text((730, 304), "关键价位", fill="#8fa9c5", font=_font(20, bold=True))
    labels = [("止损", "stop_loss"), ("支撑", "support"), ("压力", "resistance"), ("突破", "breakthrough"), ("止盈", "take_profit")]
    y = 342
    for label, key in labels:
        value = card.levels.get(key)
        if value is None:
            continue
        draw.text((730, y), label, fill="#d8e7f8", font=_font(18))
        draw.text((870, y), f"{value:.2f}", anchor="ra", fill="#f5fbff", font=_font(18, bold=True))
        y += 28

    note = card.note[:72]
    draw.text((54, 522), "告警说明", fill="#8fa9c5", font=_font(20, bold=True))
    draw.text((54, 558), note, fill="#eef6ff", font=_font(22))

    image.save(path, format="PNG")
    return path


def remove_alert_media(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    current = path.parent
    for _ in range(3):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def cleanup_alert_media(base_dir: str, retention_hours: int = 24) -> None:
    root = Path(base_dir).expanduser().resolve() / "alert-media" / "tmp"
    if not root.exists():
        return
    cutoff = time() - retention_hours * 3600
    for item in root.rglob("*"):
        if item.is_file() and item.stat().st_mtime < cutoff:
            item.unlink(missing_ok=True)


def _draw_chart(draw, chart: tuple[int, int, int, int], points: list[tuple[str, float]], current_price: float, trigger_price: float, accent: str) -> None:
    left, top, right, bottom = chart
    values = [p for _, p in points] + [current_price, trigger_price]
    lo = min(values)
    hi = max(values)
    pad = max((hi - lo) * 0.18, 0.1)
    lo -= pad
    hi += pad
    span = max(hi - lo, 0.01)
    for i in range(1, 4):
        y = top + (bottom - top) * i / 4
        draw.line((left + 14, y, right - 14, y), fill="#1d314c", width=1)
    prepared = points or [("now", current_price)]
    coords = []
    for index, (_, price) in enumerate(prepared):
        x = left + 22 + (right - left - 44) * index / max(1, len(prepared) - 1)
        y = bottom - 18 - ((price - lo) / span) * (bottom - top - 36)
        coords.append((x, y))
    if len(coords) >= 2:
        draw.line(coords, fill=accent, width=4, joint="curve")
    for x, y in coords[-3:]:
        draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill="#f7fbff")
    trigger_y = bottom - 18 - ((trigger_price - lo) / span) * (bottom - top - 36)
    draw.line((left + 14, trigger_y, right - 14, trigger_y), fill="#f2c46d", width=2)


def _draw_vertical_gradient(draw, width: int, height: int, start: str, end: str) -> None:
    s = _hex(start)
    e = _hex(end)
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = tuple(round(s[i] + (e[i] - s[i]) * ratio) for i in range(3))
        draw.line((0, y, width, y), fill=color)


def _hex(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    names: Iterable[str] = (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for name in names:
        path = Path(name)
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _safe(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)
