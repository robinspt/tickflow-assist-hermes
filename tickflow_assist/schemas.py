from __future__ import annotations


def obj(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


def schema(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {"name": name, "description": description, "parameters": obj(properties, required)}


SYMBOL = {"type": "string", "description": "A-share symbol, e.g. 002261 or 002261.SZ"}
COUNT = {"type": "integer", "description": "Number of rows/items to fetch or show"}
QUERY = {"type": "string", "description": "Natural language query"}

TOOL_SCHEMAS = {
    "add_stock": schema("add_stock", "Add an A-share symbol to TickFlow Assist watchlist, optionally with cost price and daily K-line count.", {"symbol": SYMBOL, "costPrice": {"type": "number"}, "count": COUNT}, ["symbol"]),
    "remove_stock": schema("remove_stock", "Remove a symbol from the local TickFlow Assist watchlist.", {"symbol": SYMBOL}, ["symbol"]),
    "list_watchlist": schema("list_watchlist", "List the local TickFlow Assist watchlist.", {}),
    "refresh_watchlist_names": schema("refresh_watchlist_names", "Refresh watchlist stock names from TickFlow instruments.", {}),
    "refresh_watchlist_profiles": schema("refresh_watchlist_profiles", "Refresh industry/theme profile hints from TickFlow universes and optional MX/LLM extraction.", {"symbol": SYMBOL}),
    "fetch_klines": schema("fetch_klines", "Fetch and persist daily K-line data, then calculate indicators.", {"symbol": SYMBOL, "count": COUNT}, ["symbol"]),
    "fetch_intraday_klines": schema("fetch_intraday_klines", "Fetch and persist intraday K-lines when TickFlow level supports it.", {"symbol": SYMBOL, "count": COUNT, "period": {"type": "string", "default": "1m"}}, ["symbol"]),
    "fetch_financials": schema("fetch_financials", "Fetch latest TickFlow financial snapshot for Expert API keys.", {"symbol": SYMBOL}, ["symbol"]),
    "analyze": schema("analyze", "Run LLM stock analysis using stored K-lines, indicators, realtime quote, financials and news, then persist original LanceDB result tables.", {"symbol": SYMBOL}, ["symbol"]),
    "view_analysis": schema("view_analysis", "View latest or recent saved analyses. profile can be composite, technical, financial, news, or all.", {"symbol": SYMBOL, "profile": {"type": "string"}, "limit": COUNT}, ["symbol"]),
    "backtest_key_levels": schema("backtest_key_levels", "Review recent active key level snapshots without changing database schema.", {"symbol": SYMBOL, "recentLimit": COUNT}),
    "update_all": schema("update_all", "Run one full daily update for all watchlist symbols.", {"scheduled": {"type": "boolean", "description": "Set true when invoked by Hermes cron."}}),
    "pre_market_brief": schema("pre_market_brief", "Build and persist a pre-market Jin10 brief for the watchlist, then return the message text.", {"scheduled": {"type": "boolean", "description": "Set true when invoked by Hermes cron."}}),
    "post_close_review": schema("post_close_review", "Run post-close review for all watchlist symbols and record daily update status.", {"scheduled": {"type": "boolean", "description": "Set true when invoked by Hermes cron."}}),
    "start_monitor": schema("start_monitor", "Start Hermes-thread realtime price monitoring for watchlist symbols.", {}),
    "stop_monitor": schema("stop_monitor", "Stop realtime monitoring.", {}),
    "monitor_status": schema("monitor_status", "Show realtime monitor status and watchlist summary.", {}),
    "start_daily_update": schema("start_daily_update", "Create Hermes cron jobs for daily watchlist update and post-close review.", {}),
    "stop_daily_update": schema("stop_daily_update", "Remove TickFlow Assist Hermes cron jobs for daily update and review.", {}),
    "daily_update_status": schema("daily_update_status", "Show TickFlow Assist Hermes cron daily update status.", {}),
    "test_alert": schema("test_alert", "Send a text plus PNG test alert through Hermes send_message using MEDIA.", {}),
    "query_database": schema("query_database", "Inspect LanceDB tables, schemas and stored rows without changing table fields.", {"action": {"type": "string", "enum": ["tables", "schema", "query"]}, "table": {"type": "string"}, "symbol": SYMBOL, "limit": COUNT, "fields": {"type": "array", "items": {"type": "string"}}, "sortBy": {"type": "string"}, "sortOrder": {"type": "string"}, "contains": {"type": "string"}}),
    "mx_search": schema("mx_search", "Search MX Skills news, reports, announcements, policies and events.", {"query": QUERY}, ["query"]),
    "mx_data": schema("mx_data", "Query MX official financial/market/company data with natural language.", {"query": QUERY}, ["query"]),
    "mx_select_stock": schema("mx_select_stock", "Run MX natural-language smart stock screening.", {"keyword": QUERY, "limit": COUNT}, ["keyword"]),
    "screen_stock_candidates": schema("screen_stock_candidates", "Build a small enriched candidate pool from MX smart screening plus TickFlow quotes/K-lines.", {"keyword": QUERY, "limit": COUNT, "summarize": {"type": "boolean"}}, ["keyword"]),
    "list_eastmoney_watchlist": schema("list_eastmoney_watchlist", "List Eastmoney self-select watchlist through MX self-select API.", {}),
    "sync_eastmoney_watchlist": schema("sync_eastmoney_watchlist", "Sync Eastmoney self-select watchlist into local LanceDB watchlist.", {}),
    "push_eastmoney_watchlist": schema("push_eastmoney_watchlist", "Push local watchlist symbols to Eastmoney self-select.", {}),
    "remove_eastmoney_watchlist": schema("remove_eastmoney_watchlist", "Remove a symbol from Eastmoney self-select only.", {"symbol": SYMBOL}, ["symbol"]),
    "flash_monitor_status": schema("flash_monitor_status", "Show Jin10 flash monitor runtime state, recent poll summary, storage counters, and latest flash.", {}),
    "debug_status": schema("debug_status", "Show TickFlow Assist Hermes runtime diagnostics, dependency status, paths, database path, and alert configuration.", {}),
}
