from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Iterable

from .utils import now_text

WIDTH = 960
HEIGHT = 640
MARKET_OPEN_MINUTES = 9 * 60 + 30
MORNING_CLOSE_MINUTES = 11 * 60 + 30
AFTERNOON_OPEN_MINUTES = 13 * 60
MARKET_CLOSE_MINUTES = 15 * 60
MARKET_SESSION_MINUTES = (MORNING_CLOSE_MINUTES - MARKET_OPEN_MINUTES) + (MARKET_CLOSE_MINUTES - AFTERNOON_OPEN_MINUTES)


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
    cost_price: float | None = None
    change_pct: float | None = None
    distance_pct: float | None = None
    profit_pct: float | None = None
    timestamp_label: str | None = None


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

    tone = _resolve_tone(card.label)
    points = _normalize_points(card.points, card.current_price)
    direction = _resolve_direction(card, points)
    tone_theme = _tone_theme(tone)
    direction_theme = _direction_theme(direction)

    image = _gradient_image(direction_theme["backgroundStart"], direction_theme["backgroundMid"], direction_theme["backgroundEnd"])
    image = image.convert("RGBA")
    _overlay_ellipse(image, (678, -68, 1006, 260), direction_theme["glowStrong"], 66)
    _overlay_ellipse(image, (568, 238, 952, 622), direction_theme["glowSoft"], 46)
    _overlay_ellipse(image, (-88, 312, 328, 728), tone_theme["accentSoft"], 31)
    draw = ImageDraw.Draw(image)

    frame = (24, 24, 936, 616)
    chart = (44, 214, 694, 442)
    level_panel = (714, 214, 916, 442)
    rail_left, rail_y, rail_width = 60, 594, 840

    draw.rounded_rectangle(frame, radius=24, fill=direction_theme["panelFill"], outline=direction_theme["frameStroke"], width=2)
    draw.rounded_rectangle((24, 24, 936, 36), radius=24, fill=direction_theme["ribbon"])

    _text_fit(draw, (48, 48), "TICKFLOW ALERT PREVIEW", "#88A2BF", _font(14, bold=True), 240, spacing=1)
    _text_fit(draw, (48, 84), card.name, "#F4F8FC", _font(34, bold=True), 560)
    timestamp = card.timestamp_label or now_text()
    _text_fit(draw, (48, 132), f"{card.symbol} | {timestamp}", "#8FA8C4", _font(18), 560)
    _draw_pill(draw, (48, 158), direction_theme["marketLabel"], _font(13, bold=True), direction_theme["marketPillFill"], direction_theme["marketPillText"], x_padding=12, y_padding=4)

    _draw_pill(draw, (908, 50), card.label, _font(16, bold=True), tone_theme["signalPillFill"], tone_theme["signalPillText"], x_padding=14, y_padding=5, max_width=158, anchor="ra")

    draw.text((718, 106), "当前价", fill="#8AA3BE", font=_font(15))
    draw.text((718, 156), f"{card.current_price:.2f}", fill="#F6FBFF", font=_font(34, bold=True), anchor="lb")
    for index, line in enumerate(_metric_lines(card)):
        _text_fit(draw, (718, 180 + index * 18), line, "#A8BED7", _font(14), 190, anchor="lb")

    scale = _chart_scale(card, points)
    _draw_old_style_chart(image, draw, chart, points, scale, tone_theme, direction_theme)
    _draw_level_panel(draw, level_panel, card, tone_theme, direction_theme)
    _draw_time_axis(draw, chart, points)

    draw.text((48, 486), "告警说明", fill="#8EA7C1", font=_font(14))
    _text_fit(draw, (48, 512), card.note, "#E8F1F8", _font(16, bold=True), 840)

    draw.text((48, 562), "位阶带", fill="#8EA7C1", font=_font(14))
    draw.line((rail_left, rail_y, rail_left + rail_width, rail_y), fill="#2E445D", width=8)
    _draw_rail(draw, card, rail_left, rail_width, rail_y, scale["min"], scale["max"])

    image = image.convert("RGB")

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


def _draw_old_style_chart(image, draw, chart: tuple[int, int, int, int], points: list[tuple[str, float]], scale: dict[str, float], tone_theme: dict[str, str], direction_theme: dict[str, str]) -> None:
    from PIL import Image, ImageDraw

    left, top, right, bottom = chart
    draw.rounded_rectangle(chart, radius=18, fill=direction_theme["chartPanelFill"], outline=tone_theme["panelBorder"], width=1)
    session_x = _scale_trading_x("11:30", left, right - left)
    _dashed_line(draw, (session_x, top + 10), (session_x, bottom - 10), "#3B4F68", width=1, dash=3, gap=8)
    for index in range(5):
        y = top + (index / 4) * (bottom - top)
        value = scale["max"] - (index / 4) * (scale["max"] - scale["min"])
        _dashed_line(draw, (left, y), (right, y), "#213247", width=1, dash=4, gap=8)
        draw.text((left + 12, y - 8), f"{value:.2f}", fill="#6E88A5", font=_font(12, mono=True))

    level_entries = _level_entries_for_card(scale["card"])
    for entry in level_entries:
        y = _scale_y(entry["value"], top, bottom - top, scale["min"], scale["max"])
        _dashed_line(draw, (left, y), (right, y), entry["stroke"], width=int(entry["width"]), dash=6, gap=6)

    coords = []
    use_time = _points_have_times(points)
    for index, (time_label, price) in enumerate(points):
        x = _scale_trading_x(time_label, left, right - left) if use_time else left + (right - left) * index / max(1, len(points) - 1)
        y = _scale_y(price, top, bottom - top, scale["min"], scale["max"])
        coords.append((x, y))
    if len(coords) >= 2:
        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        polygon = coords + [(coords[-1][0], bottom), (coords[0][0], bottom)]
        overlay_draw.polygon(polygon, fill=_rgba(tone_theme["accentSoft"], 70))
        image.alpha_composite(overlay)
        draw.line(coords, fill=tone_theme["accent"], width=4, joint="curve")
    x, y = coords[-1]
    draw.ellipse((x - 7, y - 7, x + 7, y + 7), fill=tone_theme["accentStrong"], outline="#F4FBFF", width=3)


def _draw_level_panel(draw, box: tuple[int, int, int, int], card: AlertCardInput, tone_theme: dict[str, str], direction_theme: dict[str, str]) -> None:
    left, top, right, bottom = box
    draw.rounded_rectangle(box, radius=18, fill=direction_theme["levelPanelFill"], outline=tone_theme["panelBorder"], width=1)
    draw.text((left + 18, top + 28), "关键价位", fill="#8EA7C1", font=_font(14))
    entries = sorted(_level_entries_for_card(card), key=lambda item: item["value"], reverse=True)
    row_gap = 32 if len(entries) > 5 else 34
    for index, entry in enumerate(entries[:5]):
        y = top + 60 + index * row_gap
        _dashed_line(draw, (left + 18, y), (left + 42, y), entry["stroke"], width=3, dash=6, gap=6)
        draw.text((left + 54, y + 5), entry["label"], fill="#DCE8F5", font=_font(14, bold=True), anchor="lm")
        draw.text((right - 18, y + 5), f"{entry['value']:.2f}", fill=entry["text"], font=_font(14, bold=True, mono=True), anchor="rm")


def _draw_time_axis(draw, chart: tuple[int, int, int, int], points: list[tuple[str, float]]) -> None:
    left, top, right, bottom = chart
    markers = [("09:30", "09:30"), ("10:30", "10:30"), ("11:30", "11:30/13:00"), ("14:00", "14:00"), ("15:00", "15:00")]
    previous = -999.0
    for time_label, label in markers:
        x = _scale_trading_x(time_label, left, right - left)
        if x - previous < 56:
            continue
        previous = x
        draw.line((x, bottom, x, bottom + 8), fill="#48627E", width=1)
        draw.text((x, bottom + 28), label, anchor="mm", fill="#7791AD", font=_font(12, mono=True))


def _draw_rail(draw, card: AlertCardInput, left: int, width: int, top: int, min_value: float, max_value: float) -> None:
    raw_markers = [
        ("止损", card.levels.get("stop_loss"), "#FF7373", 5, "#FFCFCF"),
        ("支撑", card.levels.get("support"), "#74CFFF", 5, "#DBF4FF"),
        ("现价", card.current_price, "#F7FBFF", 6, "#F7FBFF"),
        ("压力", card.levels.get("resistance"), "#FFCC6D", 5, "#FFF0C7"),
        ("突破", card.levels.get("breakthrough"), "#7DF2B4", 5, "#D8FFE8"),
        ("止盈", card.levels.get("take_profit"), "#D9ABFF", 5, "#F2E2FF"),
    ]
    grouped = _group_rail_markers([m for m in raw_markers if _finite(m[1])])
    lanes = [{"y": top - 18, "last": -999.0}, {"y": min(HEIGHT - 16, top + 38), "last": -999.0}, {"y": top - 34, "last": -999.0}]
    previous_x = -999.0
    for marker in sorted(grouped, key=lambda item: item["value"]):
        x = left + ((marker["value"] - min_value) / max(max_value - min_value, 0.01)) * width
        x = max(previous_x + 22, min(left + width, x))
        label_width = _text_width(marker["label"], _font(12, bold=True)) + 18
        lane = next((candidate for candidate in lanes if x - label_width / 2 >= candidate["last"] + 10), min(lanes, key=lambda item: item["last"]))
        lane["last"] = x + label_width / 2
        previous_x = x
        draw.line((x, top - 14, x, top + 14), fill=marker["stroke"], width=marker["width"])
        _text_fit(draw, (x, lane["y"]), marker["label"], marker["text"], _font(12, bold=True), 128, anchor="mm")


def _chart_scale(card: AlertCardInput, points: list[tuple[str, float]]) -> dict[str, float]:
    values = [card.current_price, card.trigger_price]
    values.extend(price for _, price in points)
    values.extend(value for value in card.levels.values() if _finite(value))
    min_value = min(values)
    max_value = max(values)
    padding = max((max_value - min_value) * 0.18, 0.18)
    return {"min": min_value - padding, "max": max_value + padding, "card": card}


def _level_entries_for_card(card: AlertCardInput) -> list[dict[str, float | str]]:
    entries = [
        _level_entry("止损", card.levels.get("stop_loss"), "#FF6A6A", "#FFD3D3", 2.5),
        _level_entry("支撑", card.levels.get("support"), "#78C7FF", "#DDF4FF", 2.5),
        _level_entry("压力", card.levels.get("resistance"), "#FFCC66", "#FFF0C7", 2.5),
        _level_entry("突破", card.levels.get("breakthrough"), "#7EF0B2", "#D9FFE9", 2.5),
        _level_entry("止盈", card.levels.get("take_profit"), "#D6A4FF", "#F1DFFF", 2.5),
    ]
    return [entry for entry in entries if entry]


def _level_entry(label: str, value: float | None, stroke: str, text: str, width: float):
    if not _finite(value):
        return None
    return {"label": label, "value": float(value), "stroke": stroke, "text": text, "width": width}


def _metric_lines(card: AlertCardInput) -> list[str]:
    parts = [
        f"触发位 {card.trigger_price:.2f}",
        None if card.change_pct is None else f"当日 {_pct(card.change_pct)}",
        None if card.distance_pct is None else f"偏离 {_pct(card.distance_pct)}",
        None if card.profit_pct is None else f"持仓 {_pct(card.profit_pct)}",
    ]
    values = [part for part in parts if part]
    if len(values) <= 2:
        return [" | ".join(values)]
    return [" | ".join(values[:2]), " | ".join(values[2:])]


def _resolve_tone(label: str) -> str:
    if "突破" in label:
        return "breakthrough"
    if "止损" in label:
        return "stop_loss"
    if "止盈" in label:
        return "take_profit"
    if "压力" in label:
        return "pressure"
    return "support"


def _resolve_direction(card: AlertCardInput, points: list[tuple[str, float]]) -> str:
    basis = card.change_pct
    if basis is None and points:
        first = points[0][1]
        last = points[-1][1]
        basis = ((last - first) / max(abs(first), 0.01)) * 100
    if basis is not None and basis > 0.01:
        return "up"
    if basis is not None and basis < -0.01:
        return "down"
    return "flat"


def _tone_theme(tone: str) -> dict[str, str]:
    themes = {
        "breakthrough": {"accent": "#67F3AE", "accentSoft": "#2AD97C", "accentStrong": "#9CFFCB", "panelBorder": "#2B7251", "signalPillFill": "#163F2D", "signalPillText": "#BDF6D8"},
        "stop_loss": {"accent": "#FF7C7C", "accentSoft": "#F55050", "accentStrong": "#FFC6C6", "panelBorder": "#7D3131", "signalPillFill": "#471F1F", "signalPillText": "#FFD4D4"},
        "take_profit": {"accent": "#D19BFF", "accentSoft": "#9D5CF2", "accentStrong": "#E6CCFF", "panelBorder": "#6B4398", "signalPillFill": "#38214F", "signalPillText": "#EDD7FF"},
        "pressure": {"accent": "#FFC56A", "accentSoft": "#F19E2E", "accentStrong": "#FFE0A6", "panelBorder": "#8B6130", "signalPillFill": "#4A331A", "signalPillText": "#FFE3B8"},
        "support": {"accent": "#6AD4FF", "accentSoft": "#2F8DFF", "accentStrong": "#B7ECFF", "panelBorder": "#285A8D", "signalPillFill": "#183957", "signalPillText": "#D0F2FF"},
    }
    return themes.get(tone, themes["support"])


def _direction_theme(direction: str) -> dict[str, str]:
    themes = {
        "up": {"backgroundStart": "#33080D", "backgroundMid": "#4C0F17", "backgroundEnd": "#29070C", "glowStrong": "#FF5D73", "glowSoft": "#BD2E49", "ribbon": "#FF6B81", "frameStroke": "#FF7488", "marketPillFill": "#5A1F2A", "marketPillText": "#FFE3E7", "marketLabel": "日内上涨", "panelFill": "#1D0C10", "chartPanelFill": "#271116", "levelPanelFill": "#2B1218"},
        "down": {"backgroundStart": "#0A2B19", "backgroundMid": "#114124", "backgroundEnd": "#082214", "glowStrong": "#30F289", "glowSoft": "#10A85A", "ribbon": "#42F79C", "frameStroke": "#49ED98", "marketPillFill": "#195334", "marketPillText": "#D9FFE9", "marketLabel": "日内下跌", "panelFill": "#071A10", "chartPanelFill": "#0A2317", "levelPanelFill": "#0C2519"},
        "flat": {"backgroundStart": "#081730", "backgroundMid": "#0C2144", "backgroundEnd": "#08162B", "glowStrong": "#55AFFF", "glowSoft": "#2767B1", "ribbon": "#49A5FF", "frameStroke": "#5EB0FF", "marketPillFill": "#18456F", "marketPillText": "#DBF1FF", "marketLabel": "日内走平", "panelFill": "#091427", "chartPanelFill": "#0D1B35", "levelPanelFill": "#0F203C"},
    }
    return themes.get(direction, themes["flat"])


def _gradient_image(start: str, mid: str, end: str):
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (WIDTH, HEIGHT), start)
    draw = ImageDraw.Draw(image)
    first = _rgb(start)
    middle = _rgb(mid)
    last = _rgb(end)
    for y in range(HEIGHT):
        ratio = y / max(1, HEIGHT - 1)
        if ratio <= 0.55:
            local = ratio / 0.55
            color = tuple(round(first[i] + (middle[i] - first[i]) * local) for i in range(3))
        else:
            local = (ratio - 0.55) / 0.45
            color = tuple(round(middle[i] + (last[i] - middle[i]) * local) for i in range(3))
        draw.line((0, y, WIDTH, y), fill=color)
    return image


def _overlay_ellipse(image, box: tuple[int, int, int, int], fill: str, alpha: int) -> None:
    from PIL import Image, ImageDraw

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.ellipse(box, fill=_rgba(fill, alpha))
    image.alpha_composite(overlay)


def _normalize_points(points: list[tuple[str, float]], current_price: float) -> list[tuple[str, float]]:
    raw = [(str(time_label), float(price)) for time_label, price in points if _finite(price)]
    if len(raw) < 2:
        raw = [("09:30", current_price), ("15:00", current_price)]
    normalized: list[tuple[str, float]] = []
    for time_label, price in raw:
        if normalized and _parse_clock_minutes(normalized[-1][0]) == MORNING_CLOSE_MINUTES and _parse_clock_minutes(time_label) == AFTERNOON_OPEN_MINUTES:
            normalized.append((time_label, normalized[-1][1]))
        normalized.append((time_label, price))
    return normalized


def _points_have_times(points: list[tuple[str, float]]) -> bool:
    return all(_parse_clock_minutes(time_label) is not None for time_label, _ in points)


def _scale_y(value: float, top: int, height: int, min_value: float, max_value: float) -> float:
    return top + ((max_value - value) / max(max_value - min_value, 0.01)) * height


def _scale_trading_x(time_label: str, left: int, width: int) -> float:
    minutes = _parse_clock_minutes(time_label)
    if minutes is None:
        return left
    clamped = max(MARKET_OPEN_MINUTES, min(MARKET_CLOSE_MINUTES, minutes))
    if clamped <= MORNING_CLOSE_MINUTES:
        session_minutes = clamped - MARKET_OPEN_MINUTES
    elif clamped < AFTERNOON_OPEN_MINUTES:
        session_minutes = MORNING_CLOSE_MINUTES - MARKET_OPEN_MINUTES
    else:
        session_minutes = (MORNING_CLOSE_MINUTES - MARKET_OPEN_MINUTES) + (clamped - AFTERNOON_OPEN_MINUTES)
    return left + (session_minutes / MARKET_SESSION_MINUTES) * width


def _parse_clock_minutes(value: str) -> int | None:
    parts = value.strip().split(":")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1][:2].isdigit():
        return None
    hour = int(parts[0])
    minute = int(parts[1][:2])
    if minute < 0 or minute > 59:
        return None
    return hour * 60 + minute


def _dashed_line(draw, start: tuple[float, float], end: tuple[float, float], fill: str, width: int = 1, dash: int = 6, gap: int = 6) -> None:
    x1, y1 = start
    x2, y2 = end
    length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    if length == 0:
        return
    dx = (x2 - x1) / length
    dy = (y2 - y1) / length
    distance = 0.0
    while distance < length:
        segment = min(dash, length - distance)
        sx = x1 + dx * distance
        sy = y1 + dy * distance
        ex = x1 + dx * (distance + segment)
        ey = y1 + dy * (distance + segment)
        draw.line((sx, sy, ex, ey), fill=fill, width=width)
        distance += dash + gap


def _group_rail_markers(markers: list[tuple[str, float, str, int, str]]) -> list[dict[str, float | str | int]]:
    groups: dict[str, list[tuple[str, float, str, int, str]]] = {}
    for marker in markers:
        groups.setdefault(f"{marker[1]:.4f}", []).append(marker)
    output = []
    for group in groups.values():
        labels = "/".join(item[0] for item in group)
        value = group[0][1]
        label = f"{labels} {value:.2f}"
        if len(group) == 1:
            output.append({"label": label, "value": value, "stroke": group[0][2], "width": group[0][3], "text": group[0][4]})
        else:
            output.append({"label": label, "value": value, "stroke": "#F3F7FB", "width": 6, "text": "#F7FBFF"})
    return output


def _text_fit(draw, xy: tuple[float, float], text: str, fill: str, font, max_width: int, anchor: str | None = None, spacing: int = 0) -> None:
    fitted = _ellipsize(draw, text, font, max_width)
    draw.text(xy, fitted, fill=fill, font=font, anchor=anchor, spacing=spacing)


def _draw_pill(draw, xy: tuple[float, float], text: str, font, fill: str, text_fill: str, x_padding: int, y_padding: int, max_width: int | None = None, anchor: str = "la") -> None:
    max_text_width = max_width - x_padding * 2 if max_width else None
    fitted = _ellipsize(draw, text, font, max_text_width) if max_text_width else text
    left, top, right, bottom = draw.textbbox((0, 0), fitted, font=font)
    text_width = right - left
    text_height = bottom - top
    width = text_width + x_padding * 2
    height = text_height + y_padding * 2
    x, y = xy
    if anchor == "ra":
        box = (x - width, y, x, y + height)
        text_xy = (x - width / 2, y + height / 2)
    else:
        box = (x, y, x + width, y + height)
        text_xy = (x + width / 2, y + height / 2)
    draw.rounded_rectangle(box, radius=max(8, int(height / 2)), fill=fill)
    draw.text(text_xy, fitted, anchor="mm", fill=text_fill, font=font)


def _ellipsize(draw, text: str, font, max_width: int) -> str:
    if _text_width(text, font) <= max_width:
        return text
    suffix = "..."
    remaining = text
    while remaining and _text_width(remaining + suffix, font) > max_width:
        remaining = remaining[:-1]
    return (remaining + suffix) if remaining else suffix


def _text_width(text: str, font) -> int:
    try:
        left, _, right, _ = font.getbbox(text)
        return right - left
    except Exception:
        return len(text) * 10


def _pct(value: float) -> str:
    return f"{'+' if value >= 0 else ''}{value:.2f}%"


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and value == value and value not in (float("inf"), float("-inf"))


def _rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _rgba(value: str, alpha: int) -> tuple[int, int, int, int]:
    red, green, blue = _rgb(value)
    return red, green, blue, alpha


def _font(size: int, bold: bool = False, mono: bool = False):
    from PIL import ImageFont

    names: Iterable[str] = (
        "/usr/share/fonts/truetype/jetbrains-mono/JetBrainsMono-Bold.ttf" if mono and bold else "",
        "/usr/share/fonts/truetype/jetbrains-mono/JetBrainsMono-Regular.ttf" if mono else "",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for name in names:
        if not name:
            continue
        path = Path(name)
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _safe(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)
