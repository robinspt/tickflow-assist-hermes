from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def calculate_indicators(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        raise ValueError("K-line data must contain at least 1 row")
    df = pd.DataFrame(rows).copy()
    sort_cols = [col for col in ["timestamp", "trade_date", "trade_time"] if col in df.columns]
    if sort_cols:
        df = df.sort_values(sort_cols, kind="stable").reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)

    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]
    for window in [5, 10, 20, 60]:
        df[f"ma{window}"] = close.rolling(window=window, min_periods=1).mean()

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    kdj = _kdj(high, low, close)
    df["kdj_k"] = kdj["k"]
    df["kdj_d"] = kdj["d"]
    df["kdj_j"] = kdj["j"]
    for window in [6, 12, 24]:
        df[f"rsi_{window}"] = _rsi(close, window)

    typical = (high + low + close) / 3.0
    ma_typical = typical.rolling(14, min_periods=1).mean()
    mean_dev = typical.rolling(14, min_periods=1).apply(lambda x: np.mean(np.abs(x - np.mean(x))), raw=True)
    df["cci"] = (typical - ma_typical) / (0.015 * mean_dev.replace(0, np.nan))
    for window in [6, 12, 24]:
        ma = close.rolling(window, min_periods=1).mean()
        df[f"bias_{window}"] = (close - ma) / ma * 100.0

    plus_di, minus_di, adx = _dmi(high, low, close)
    df["plus_di"] = plus_di
    df["minus_di"] = minus_di
    df["adx"] = adx
    mid = close.rolling(20, min_periods=1).mean()
    std = close.rolling(20, min_periods=1).std(ddof=0)
    df["boll_mid"] = mid
    df["boll_upper"] = mid + 2 * std
    df["boll_lower"] = mid - 2 * std

    keep = [
        "symbol", "trade_date", "ma5", "ma10", "ma20", "ma60", "macd", "macd_signal",
        "macd_hist", "kdj_k", "kdj_d", "kdj_j", "rsi_6", "rsi_12", "rsi_24",
        "cci", "bias_6", "bias_12", "bias_24", "plus_di", "minus_di", "adx",
        "boll_upper", "boll_mid", "boll_lower",
    ]
    out = df[keep].replace({np.nan: None})
    return out.to_dict(orient="records")


def _rsi(close: pd.Series, window: int) -> pd.Series:
    delta = close.diff().fillna(0.0)
    gain = delta.clip(lower=0.0).ewm(alpha=1 / window, adjust=False).mean()
    loss = delta.abs().ewm(alpha=1 / window, adjust=False).mean()
    return (gain / loss.replace(0, np.nan) * 100.0).mask(loss.eq(0.0), 50.0)


def _kdj(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.DataFrame:
    low_min = low.rolling(9, min_periods=9).min()
    high_max = high.rolling(9, min_periods=9).max()
    rsv = (close - low_min) / (high_max - low_min).replace(0, np.nan) * 100.0
    k_values, d_values = [], []
    prev_k, prev_d = 50.0, 50.0
    for value in rsv:
        if pd.isna(value):
            k_values.append(np.nan)
            d_values.append(np.nan)
            continue
        prev_k = (2 * prev_k + float(value)) / 3
        prev_d = (2 * prev_d + prev_k) / 3
        k_values.append(prev_k)
        d_values.append(prev_d)
    k = pd.Series(k_values, index=close.index)
    d = pd.Series(d_values, index=close.index)
    return pd.DataFrame({"k": k, "d": d, "j": 3 * k - 2 * d})


def _dmi(high: pd.Series, low: pd.Series, close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = pd.concat([(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14, min_periods=1).mean().replace(0, np.nan)
    plus_di = plus_dm.rolling(14, min_periods=1).sum() / atr * 100.0
    minus_di = minus_dm.rolling(14, min_periods=1).sum() / atr * 100.0
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100.0
    return plus_di, minus_di, dx.rolling(14, min_periods=1).mean()
