from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone, timedelta
from typing import Any

CHINA_TZ = timezone(timedelta(hours=8))


def now_cn() -> datetime:
    return datetime.now(CHINA_TZ)


def now_text() -> str:
    return now_cn().strftime("%Y-%m-%d %H:%M:%S")


def today_text() -> str:
    return now_cn().strftime("%Y-%m-%d")


def normalize_symbol(value: str) -> str:
    text = str(value or "").strip().upper()
    if not text:
        raise ValueError("symbol is required")
    text = text.replace(" ", "")
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", text):
        return text
    code = re.sub(r"\D", "", text)
    if not re.fullmatch(r"\d{6}", code):
        return text
    if code.startswith(("6", "5", "9")):
        return f"{code}.SH"
    if code.startswith(("8", "4")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def symbol_code(symbol: str) -> str:
    return normalize_symbol(symbol).split(".")[0]


def parse_positive_float(value: Any, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    parsed = float(value)
    if parsed <= 0:
        raise ValueError("number must be greater than 0")
    return parsed


def parse_positive_int(value: Any, default: int, maximum: int | None = None) -> int:
    if value in (None, ""):
        parsed = default
    else:
        parsed = int(float(value))
    if parsed <= 0:
        raise ValueError("number must be greater than 0")
    return min(parsed, maximum) if maximum else parsed


def first_nonempty(*values: Any) -> str:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def pct(value: float | None) -> str:
    return "-" if value is None else f"{value:+.2f}%"


def fmt_price(value: Any) -> str:
    number = safe_float(value)
    return "-" if number is None else f"{number:.2f}"


def hash_key(*parts: Any) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def is_trading_time(dt: datetime | None = None) -> bool:
    current = dt or now_cn()
    if current.weekday() >= 5:
        return False
    hhmm = current.hour * 100 + current.minute
    return 930 <= hhmm <= 1130 or 1300 <= hhmm <= 1500


def get_nested(value: Any, *keys: str) -> Any:
    cur = value
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
