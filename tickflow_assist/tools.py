from __future__ import annotations

import json
from typing import Any, Callable

from .core import App
from .utils import parse_positive_float, parse_positive_int

APP = App()


def set_context(ctx: Any) -> None:
    APP.set_context(ctx)


def _ok(text: str, **extra: Any) -> str:
    return json.dumps({"ok": True, "text": text, **extra}, ensure_ascii=False)


def _err(error: Exception | str) -> str:
    return json.dumps({"ok": False, "error": str(error), "text": f"⚠️ {error}"}, ensure_ascii=False)


def _wrap(fn: Callable[[dict[str, Any]], str], args: dict[str, Any]) -> str:
    try:
        return _ok(fn(args or {}))
    except Exception as exc:
        return _err(exc)


def add_stock(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.add_stock(a.get("symbol", ""), parse_positive_float(a.get("costPrice")), parse_positive_int(a.get("count") or a.get("klineCount"), 90, 500)), args)


def remove_stock(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.remove_stock(a.get("symbol", "")), args)


def list_watchlist(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.list_watchlist(), args)


def refresh_watchlist_names(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.refresh_watchlist_names(), args)


def refresh_watchlist_profiles(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.refresh_watchlist_profiles(a.get("symbol")), args)


def fetch_klines(args: dict, **kwargs) -> str:
    def run(a):
        rows = APP.fetch_klines(a.get("symbol", ""), parse_positive_int(a.get("count"), 90, 1000), persist=True)
        if rows:
            APP.store.replace_where("indicators", f"symbol = '{rows[0]['symbol']}'", __import__("tickflow_assist.indicators", fromlist=["calculate_indicators"]).calculate_indicators(rows))
        return f"📊 已获取日K: {len(rows)} 根\n区间: {rows[0]['trade_date']} ~ {rows[-1]['trade_date']}" if rows else "未获取到日K数据。"
    return _wrap(run, args)


def fetch_intraday_klines(args: dict, **kwargs) -> str:
    return _wrap(lambda a: f"📈 已获取分钟K: {len(APP.fetch_intraday(a.get('symbol', ''), parse_positive_int(a.get('count'), 240, 2000), a.get('period') or '1m'))} 根", args)


def fetch_financials(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.fetch_financials(a.get("symbol", "")), args)


def analyze(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.analyze(a.get("symbol", "")), args)


def view_analysis(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.view_analysis(a.get("symbol", ""), a.get("profile") or a.get("view") or "composite", parse_positive_int(a.get("limit") or a.get("count"), 1, 20)), args)


def backtest_key_levels(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.backtest_key_levels(a.get("symbol"), parse_positive_int(a.get("recentLimit") or a.get("limit"), 20, 200)), args)


def update_all(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.update_all(), args)


def start_monitor(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.start_monitor(), args)


def stop_monitor(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.stop_monitor(), args)


def monitor_status(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.monitor_status(), args)


def start_daily_update(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.start_daily_update(), args)


def stop_daily_update(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.stop_daily_update(), args)


def daily_update_status(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.daily_update_status(), args)


def test_alert(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.test_alert(), args)


def query_database(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.query_database(a.get("action") or "tables", a.get("table"), a.get("symbol"), parse_positive_int(a.get("limit"), 10, 100), a.get("fields"), a.get("sortBy"), a.get("sortOrder") or "desc", a.get("contains")), args)


def mx_search(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.mx_search_text(a.get("query") or a.get("keyword") or ""), args)


def mx_data(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.mx_data_text(a.get("query") or a.get("toolQuery") or ""), args)


def mx_select_stock(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.mx_select_text(a.get("keyword") or a.get("query") or "", parse_positive_int(a.get("limit") or a.get("pageSize"), 20, 100)), args)


def screen_stock_candidates(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.screen_candidates(a.get("keyword") or a.get("query") or "", parse_positive_int(a.get("limit"), 3, 8), bool(a.get("summarize") or a.get("llm"))), args)


def list_eastmoney_watchlist(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.eastmoney_watchlist(), args)


def sync_eastmoney_watchlist(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.sync_eastmoney_watchlist(), args)


def push_eastmoney_watchlist(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.push_eastmoney_watchlist(), args)


def remove_eastmoney_watchlist(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.remove_eastmoney_watchlist(a.get("symbol", "")), args)


def flash_monitor_status(args: dict, **kwargs) -> str:
    return _wrap(lambda a: APP.flash_monitor_status(), args)


HANDLERS = {
    name: globals()[name]
    for name in [
        "add_stock", "remove_stock", "list_watchlist", "refresh_watchlist_names",
        "refresh_watchlist_profiles", "fetch_klines", "fetch_intraday_klines",
        "fetch_financials", "analyze", "view_analysis", "backtest_key_levels",
        "update_all", "start_monitor", "stop_monitor", "monitor_status",
        "start_daily_update", "stop_daily_update", "daily_update_status",
        "test_alert", "query_database", "mx_search", "mx_data", "mx_select_stock",
        "screen_stock_candidates", "list_eastmoney_watchlist",
        "sync_eastmoney_watchlist", "push_eastmoney_watchlist",
        "remove_eastmoney_watchlist", "flash_monitor_status",
    ]
}
