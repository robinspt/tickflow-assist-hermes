from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .alert_media import AlertCardInput, remove_alert_media, write_alert_card
from .clients import Jin10Client, MxClient, TickFlowClient, call_llm
from .config import Config, load_config, supports_financial, supports_intraday
from .storage import LanceStore, SCHEMAS, json_text
from .utils import fmt_price, hash_key, normalize_symbol, now_cn, now_text, pct, safe_float, safe_int, symbol_code, today_text


ANALYSIS_SYSTEM = """你是一位A股综合分析师。基于提供的日K、技术指标、实时行情、财务和资讯材料，输出中文分析。
要求：先给100-150字核心摘要，再分“技术面与关键位 / 基本面结论 / 资讯催化与风险 / 共振或冲突与交易判断”展开。
最后必须输出一个 ```json 代码块，字段包含 current_price, stop_loss, breakthrough, support, cost_level, resistance, take_profit, gap, target, round_number, score。
不要编造未提供的数据；不构成投资建议。"""

WATCHLIST_PROFILE_EXTRACTION_SYSTEM = """你是A股证券资料结构化抽取助手。

你的唯一任务：根据给定的妙想搜索结果，提取该股票的行业分类与概念板块，并严格输出 JSON。

硬性要求：
1. 只能依据提供的资料，不得编造。
2. 只输出 JSON 对象或 ```json 代码块，不要输出解释文字。
3. JSON 结构固定为：
{
  "sector": string | null,
  "themes": string[],
  "confidence": "low" | "medium" | "high"
}
4. sector 优先提取申万行业/行业分类，保留完整层级；没有可靠信息时填 null。
5. themes 尽量完整列出概念板块，去重后输出数组；优先保留明确的概念/题材/板块名称，最多保留 10 个。
6. themes 中不要输出泛词，例如公司新闻、最新公告、市场快讯；也不要输出等。
7. 若资料中是组合表达，拆成独立概念更优，例如华为昇腾 / 华为昇思应拆成两个数组项。
8. 若资料仅出现业务描述而没有足够证据支持概念标签，不要强行扩写。
9. confidence 仅反映你对提取结果的把握，不要附加解释。"""

PRE_MARKET_BRIEF_SYSTEM = """你是一位A股开盘前资讯简报编辑。基于金十数据整理快讯、自选股行业/题材和命中信息，生成适合开盘前阅读的中文简报。
要求：
1. 先给 3-5 条重大要闻，再给自选相关、潜在机会、风险提示、开盘前关注清单。
2. 不编造未提供的股票、板块、政策或数据。
3. 重点写清楚这些消息可能如何影响风险偏好、题材扩散和自选股观察点。
4. 输出可直接投递到 Telegram/Discord 的纯文本。"""

POST_CLOSE_REVIEW_SYSTEM = """你是一位A股收盘复盘分析师，需要在收盘后同时完成“昨日关键位验证 + 今日盘面复盘 + 明日关键位处理决定”。
输出要求：
1. 正文按“昨日关键位验证 / 今日盘面 / 大盘与板块 / 新闻与公告 / 明日关键位处理 / 操作建议”组织。
2. “昨日关键位验证”必须严格依据输入的验证结果，不得改写成与数据冲突的结论。
3. “明日关键位处理”必须明确给出四选一结论：keep / adjust / recompute / invalidate。
4. 最后输出一个 ```json 代码块，字段包含 session_summary, market_sector_summary, news_summary, decision, decision_reason, action_advice, market_bias, sector_bias, news_impact, levels。
5. levels 若不为 null，必须包含 current_price, stop_loss, breakthrough, support, cost_level, resistance, take_profit, gap, target, round_number, score。
不要编造未提供的数据；不构成投资建议。"""

PRE_MARKET_BRIEF_KEYWORD = "金十数据整理"
PRE_MARKET_BRIEF_READY_TIME = "09:20"
PRE_MARKET_BRIEF_EXPIRE_TIME = "09:30"
DAILY_UPDATE_READY_TIME = "15:25"
POST_CLOSE_REVIEW_READY_TIME = "20:00"
DAILY_SCHEDULE_VERSION = 2
DAILY_UPDATE_LOOP_INTERVAL_SECONDS = 60
MONITOR_STALE_GRACE_SECONDS = 90
DAILY_UPDATE_STALE_GRACE_SECONDS = 20 * 60
SYSTEM_SESSION_ALERT_SYMBOL = "__system_session__"
UNIVERSE_CACHE_REFRESH_SECONDS = 24 * 60 * 60
UNIVERSE_BATCH_SIZE = 50
SHENWAN_UNIVERSE_PATTERN = re.compile(r"^CN_Equity_(SW[123])_(\d{6})$")
FLASH_MAX_PAGES_PER_POLL = 5
FLASH_BACKFILL_PAGES_PER_POLL = 1
FLASH_INITIAL_SEED_PAGES = 3
FLASH_PRUNE_INTERVAL_SECONDS = 6 * 60 * 60
FLASH_ALERT_FRESHNESS_GRACE_SECONDS = 30
FLASH_NOISE_PATTERNS = [re.compile(r"^金十图示[:：]"), re.compile(r"交易学院正在直播中")]
FLASH_HIGH_IMPORTANCE_KEYWORDS = ["重组", "减持", "增持", "业绩预告", "业绩快报", "中标", "签署", "订单", "停牌", "复牌", "监管", "问询", "处罚", "回购"]
MARKET_OVERVIEW_FLASH_KEYWORDS = ["港股收评", "每日投行/机构观点梳理", "A股每日市场要闻回顾", "A 股每日市场要闻回顾"]
DEFAULT_MARKET_INDEXES = [
    {"symbol": "000001.SH", "name": "上证指数"},
    {"symbol": "399001.SZ", "name": "深证成指"},
]
DAILY_UPDATE_KLINE_DAYS = 90
DAILY_UPDATE_STOCK_ADJUST = "forward"
LEVEL_BUFFER = 0.005
FLASH_DEFAULT_STATE = {
    "initialized": False,
    "lastSeenKey": None,
    "lastSeenPublishedAt": None,
    "lastSeenUrl": None,
    "backfillCursor": None,
    "runtimeHost": None,
    "runtimeObservedAt": None,
    "lastHeartbeatAt": None,
    "lastPollAt": None,
    "lastPollStored": 0,
    "lastBackfillStored": 0,
    "lastPollCandidates": 0,
    "lastPollAlerts": 0,
    "lastPrunedAt": None,
    "lastLoopError": None,
    "lastLoopErrorAt": None,
}

DAILY_DEFAULT_STATE = {
    "running": False,
    "scheduleVersion": None,
    "startedAt": None,
    "lastStoppedAt": None,
    "runtimeHost": None,
    "runtimeObservedAt": None,
    "lastHeartbeatAt": None,
    "jobIds": [],
    "lastPreMarketAttemptAt": None,
    "lastPreMarketAttemptDate": None,
    "lastPreMarketSuccessAt": None,
    "lastPreMarketSuccessDate": None,
    "lastPreMarketResultType": None,
    "lastPreMarketResultSummary": None,
    "preMarketConsecutiveFailures": 0,
    "lastAttemptAt": None,
    "lastAttemptDate": None,
    "lastSuccessAt": None,
    "lastSuccessDate": None,
    "lastResultType": None,
    "lastResultSummary": None,
    "consecutiveFailures": 0,
    "lastReviewAttemptAt": None,
    "lastReviewAttemptDate": None,
    "lastReviewSuccessAt": None,
    "lastReviewSuccessDate": None,
    "lastReviewResultType": None,
    "lastReviewResultSummary": None,
    "reviewConsecutiveFailures": 0,
    "lastError": None,
    "lastErrorAt": None,
    "disabledByUser": False,
    "lastNotificationAttemptAt": None,
    "lastNotificationSentAt": None,
    "lastNotificationTarget": None,
    "lastNotificationError": None,
    "lastNotificationErrorAt": None,
}


class App:
    def __init__(self, config: Config | None = None):
        self.plugin_root = Path(__file__).resolve().parents[1]
        self.config = config or load_config(self.plugin_root)
        self.store = LanceStore(self.config.database_path)
        self.tickflow = TickFlowClient(self.config)
        self.mx = MxClient(self.config)
        self.jin10 = Jin10Client(self.config)
        self.ctx: Any = None
        self.monitor_thread: threading.Thread | None = None
        self.monitor_stop = threading.Event()
        self.daily_thread: threading.Thread | None = None
        self.daily_stop = threading.Event()
        self.flash_thread: threading.Thread | None = None
        self.flash_stop = threading.Event()
        self.state_lock = threading.RLock()
        self._calendar_days: set[str] | None = None

    def set_context(self, ctx: Any) -> None:
        self.ctx = ctx

    def watchlist(self) -> list[dict[str, Any]]:
        return sorted(self.store.rows("watchlist"), key=lambda r: str(r.get("addedAt") or ""))

    def add_stock(self, symbol: str, cost_price: float | None = None, count: int = 90) -> str:
        symbol = normalize_symbol(symbol)
        existing = {row["symbol"]: row for row in self.watchlist()}
        name = existing.get(symbol, {}).get("name") or self._instrument_name(symbol)
        row = {
            "symbol": symbol,
            "name": name or symbol,
            "costPrice": cost_price or existing.get(symbol, {}).get("costPrice") or 0,
            "addedAt": existing.get(symbol, {}).get("addedAt") or now_text(),
            "sector": existing.get(symbol, {}).get("sector"),
            "themes": existing.get(symbol, {}).get("themes"),
            "themeQuery": existing.get(symbol, {}).get("themeQuery"),
            "themeUpdatedAt": existing.get(symbol, {}).get("themeUpdatedAt"),
        }
        rows = [item for item in existing.values() if item["symbol"] != symbol] + [row]
        self.store.replace_where("watchlist", f"symbol = '{symbol}'", [row])
        lines = [f"✅ 已加入自选: {row['name']}（{symbol}）", f"成本价: {fmt_price(row['costPrice']) if row['costPrice'] else '未设置'}"]
        try:
            kline_rows = self.fetch_klines(symbol, count=count, persist=True)
            from .indicators import calculate_indicators

            indicators = calculate_indicators(kline_rows)
            self.store.replace_where("indicators", f"symbol = '{symbol}'", indicators)
            lines.extend([f"📊 已自动获取日K: {len(kline_rows)} 根", f"区间: {kline_rows[0]['trade_date']} ~ {kline_rows[-1]['trade_date']}", f"最新收盘: {kline_rows[-1]['close']:.2f}", "🔧 技术指标已计算并写入数据库"])
        except Exception as exc:
            lines.append(f"⚠️ 日K/指标更新失败: {exc}")
        return "\n".join(lines)

    def remove_stock(self, symbol: str) -> str:
        symbol = normalize_symbol(symbol)
        if not self.store.has_table("watchlist"):
            return "✅ 自选列表为空"
        self.store.open("watchlist").delete(f"symbol = '{symbol}'")
        return f"🛑 已从自选移除: {symbol}"

    def list_watchlist(self) -> str:
        rows = self.watchlist()
        if not rows:
            return "自选列表为空。"
        lines = [f"📋 自选列表（{len(rows)}只）:"]
        for item in rows:
            details = []
            sector = _clean_profile_text(item.get("sector"))
            themes = _join_theme_labels(_normalize_theme_labels(item.get("themes"), company_name=item.get("name")))
            if sector:
                details.append(f"行业: {sector}")
            if themes:
                details.append(f"题材: {themes}")
            suffix = f" | {' | '.join(details)}" if details else ""
            lines.append(f"• {item.get('name') or item['symbol']}（{item['symbol']}） 成本: {fmt_price(item.get('costPrice')) if item.get('costPrice') else '未设置'}{suffix}")
        return "\n".join(lines)

    def refresh_watchlist_names(self) -> str:
        rows = self.watchlist()
        if not rows:
            return "自选列表为空。"
        instruments = {item.get("symbol"): item for item in self.tickflow.instruments([row["symbol"] for row in rows])}
        updated = []
        for row in rows:
            inst = instruments.get(row["symbol"]) or {}
            row["name"] = inst.get("name") or inst.get("display_name") or row.get("name") or row["symbol"]
            updated.append(row)
        self.store.replace_where("watchlist", "symbol != ''", updated)
        return f"✅ 已刷新自选股名称: {len(updated)} 只"

    def refresh_watchlist_profiles(self, symbol: str | None = None) -> str:
        all_rows = self.watchlist()
        target_rows = [row for row in all_rows if not symbol or row["symbol"] == normalize_symbol(symbol)]
        if not target_rows:
            return "没有需要刷新的自选股。"
        updated: list[dict[str, Any]] = []
        rechecked: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for row in target_rows:
            original = dict(row)
            try:
                profile = self._resolve_watchlist_profile(row)
                current_themes = _normalize_theme_labels(row.get("themes"), company_name=row.get("name"))
                next_themes = profile["themes"] or current_themes
                row["sector"] = profile["sector"] or _clean_profile_text(row.get("sector"))
                row["themes"] = _join_theme_labels(next_themes) or None
                row["themeQuery"] = profile["themeQuery"] or row.get("themeQuery")
                row["themeUpdatedAt"] = profile["themeUpdatedAt"] or now_text()
                if _profile_changed(original, row):
                    updated.append(row)
                else:
                    rechecked.append(row)
            except Exception as exc:
                row["sector"] = _clean_profile_text(row.get("sector"))
                row["themes"] = _join_theme_labels(_normalize_theme_labels(row.get("themes"), company_name=row.get("name"))) or None
                failed.append({"symbol": row["symbol"], "name": row.get("name") or row["symbol"], "error": str(exc)})
        self.store.replace_where("watchlist", "symbol != ''", all_rows)
        lines = [
            f"✅ 行业/题材刷新完成: 目标 {len(target_rows)} 只 | 资料更新 {len(updated)} | 已复核 {len(rechecked)} | 失败 {len(failed)}",
            "来源: TickFlow universes（申万行业）" + (" + MX/LLM（概念题材）" if self._can_extract_profile_with_llm() else ""),
        ]
        for item in updated[:10]:
            lines.append(_format_profile_refresh_line(item))
        if failed:
            lines.append("失败:")
            for item in failed[:10]:
                lines.append(f"• {item['name']}（{item['symbol']}）: {item['error']}")
        return "\n".join(lines)

    def fetch_klines(self, symbol: str, count: int = 90, persist: bool = True, adjust: str = "forward") -> list[dict[str, Any]]:
        symbol = normalize_symbol(symbol)
        rows = self.tickflow.klines(symbol, count=count, adjust=adjust)
        if persist and rows:
            self.store.replace_where("klines_daily", f"symbol = '{symbol}'", rows)
        return rows

    def fetch_intraday(self, symbol: str, count: int = 240, period: str = "1m", persist: bool = True) -> list[dict[str, Any]]:
        if not supports_intraday(self.config.tickflow_api_key_level):
            raise RuntimeError(f"当前 TickFlow 级别 {self.config.tickflow_api_key_level} 不支持分钟K")
        symbol = normalize_symbol(symbol)
        rows = self.tickflow.intraday(symbol, count=count, period=period)
        if persist and rows:
            self.store.replace_where("klines_intraday", f"symbol = '{symbol}' AND period = '{period}'", rows)
        return rows

    def fetch_financials(self, symbol: str) -> str:
        if not supports_financial(self.config.tickflow_api_key_level):
            return "当前 TickFlow API Key 级别非 Expert，完整财务数据不可用；可使用 mx_data 查询 lite 财务数据。"
        snap = self.tickflow.financial_snapshot(symbol)
        lines = [f"💼 财务数据: {normalize_symbol(symbol)}"]
        for key in ["income", "metrics", "cashFlow", "balanceSheet"]:
            rows = snap.get(key) or []
            lines.append(f"{key}: {len(rows)} 条")
            if rows:
                latest = rows[0]
                lines.append(json.dumps(latest, ensure_ascii=False)[:800])
        return "\n".join(lines)

    def update_all(self, scheduled: bool = False) -> str:
        today = today_text()
        attempted_at = now_text()
        if scheduled and not self._is_trading_day(today):
            message = f"{today} 非交易日，已跳过定时日更。"
            self._record_daily_update_result("skipped", message, attempted_at, today)
            return "[SILENT] " + message
        rows = self.watchlist()
        lines = [
            f"📊 收盘更新: {len(rows)} 只股票 + {len(DEFAULT_MARKET_INDEXES)} 个指数, 获取 {DAILY_UPDATE_KLINE_DAYS} 天日K与当日分钟K (个股复权: {DAILY_UPDATE_STOCK_ADJUST})",
            f"🔑 TickFlow API Key Level: {_format_api_key_level(self.config.tickflow_api_key_level)}",
            "",
            "📈 指数更新:",
        ]
        index_ok, index_failed = 0, 0
        for item in DEFAULT_MARKET_INDEXES:
            result = self._update_market_target(item["symbol"], item["name"], "index", DAILY_UPDATE_KLINE_DAYS, "none")
            lines.append(result["line"])
            if result["ok"]:
                index_ok += 1
            else:
                index_failed += 1

        stock_ok, stock_failed = 0, 0
        if not rows:
            lines.extend(["", "📋 关注列表为空，已跳过个股更新"])
        else:
            lines.extend(["", "📋 个股更新:"])
            for item in rows:
                result = self._update_market_target(
                    item["symbol"],
                    item.get("name") or item["symbol"],
                    "stock",
                    DAILY_UPDATE_KLINE_DAYS,
                    DAILY_UPDATE_STOCK_ADJUST,
                )
                lines.append(result["line"])
                if result["ok"]:
                    stock_ok += 1
                else:
                    stock_failed += 1
        lines.append(f"🏁 完成: 指数 {index_ok} 成功, {index_failed} 失败 | 个股 {stock_ok} 成功, {stock_failed} 失败 (共 {len(rows)} 只)")
        message = "\n".join(lines)
        self._record_daily_update_result("success" if index_ok + stock_ok > 0 else "failed", message, attempted_at, today)
        return message

    def _update_market_target(self, symbol: str, name: str, kind: str, days: int, adjust: str) -> dict[str, Any]:
        symbol = normalize_symbol(symbol)
        try:
            klines = self.fetch_klines(symbol, count=days, persist=True, adjust=adjust)
            if not klines:
                return {"ok": False, "line": f"❌ {name}（{symbol}）: 返回数据为空"}
            from .indicators import calculate_indicators

            self.store.replace_where("indicators", f"symbol = '{symbol}'", calculate_indicators(klines))
            intraday_summary = f"分钟K 已跳过（API Key Level={_format_api_key_level(self.config.tickflow_api_key_level)}）"
            if supports_intraday(self.config.tickflow_api_key_level):
                try:
                    intraday_rows = self.fetch_intraday(symbol, count=240)
                    intraday_summary = f"分钟K {len(intraday_rows)} 根"
                except Exception as exc:
                    intraday_summary = f"分钟K 更新失败，已跳过（{exc}）"
            latest = klines[-1]
            scope = "指数" if kind == "index" else "个股"
            return {
                "ok": True,
                "line": f"✅ {name}（{symbol}）: {scope}日K {len(klines)} 根, {intraday_summary}, 最新 {latest.get('trade_date') or '-'} 收盘 {fmt_price(latest.get('close'))}",
            }
        except Exception as exc:
            return {"ok": False, "line": f"❌ {name}（{symbol}）: {exc}"}

    def pre_market_brief(self, scheduled: bool = False) -> str:
        today = today_text()
        attempted_at = now_text()
        if scheduled and not self._is_trading_day(today):
            message = f"{today} 非交易日，已跳过盘前资讯简报。"
            self._record_pre_market_result("skipped", message, attempted_at, today)
            return "[SILENT] " + message
        rows = self.watchlist()
        if not rows:
            message = "🚫 开盘前资讯简报已跳过：关注列表为空。"
            self._record_pre_market_result("skipped", message, attempted_at, today)
            return ("[SILENT] " if scheduled else "") + message
        if not self.jin10.configured():
            message = "🚫 开盘前资讯简报已跳过：Jin10 MCP 未配置，请设置 jin10ApiToken。"
            self._record_pre_market_result("skipped", message, attempted_at, today)
            return ("[SILENT] " if scheduled else "") + message
        try:
            window = _pre_market_window()
            sync_warning = None
            try:
                self._sync_pre_market_flash_window(window)
            except Exception as exc:
                sync_warning = f"本轮金十同步异常，已使用本地缓存生成简报: {exc}"
            flashes = [
                row for row in self.store.rows("jin10_flash")
                if window["startTs"] <= int(row.get("published_ts") or 0) <= window["endTs"]
                and PRE_MARKET_BRIEF_KEYWORD in str(row.get("content") or "")
            ]
            flashes = sorted(flashes, key=lambda row: int(row.get("published_ts") or 0), reverse=True)
            message = self._build_pre_market_brief_text(window, rows, flashes, sync_warning=sync_warning)
            self._record_pre_market_result("success", message, attempted_at, today)
            return message
        except Exception as exc:
            message = f"⚠️ 开盘前资讯简报失败: {exc}"
            self._record_pre_market_result("failed", message, attempted_at, today)
            if scheduled:
                return message
            raise

    def post_close_review(self, scheduled: bool = False) -> str:
        today = today_text()
        attempted_at = now_text()
        if scheduled and not self._is_trading_day(today):
            message = f"{today} 非交易日，已跳过收盘复盘。"
            self._record_review_result("skipped", message, attempted_at, today)
            return "[SILENT] " + message
        rows = self.watchlist()
        if not rows:
            message = "自选列表为空，已跳过收盘复盘。"
            self._record_review_result("skipped", message, attempted_at, today)
            return ("[SILENT] " if scheduled else "") + message
        entries: list[dict[str, Any]] = []
        detail_messages: list[str] = []
        market_overview = self._post_close_market_overview(today)
        for item in rows:
            try:
                entry = self._post_close_review_item(item)
                entries.append(entry)
                detail_messages.append(entry["message"])
            except Exception as exc:
                message = _format_post_close_failure_message(item, str(exc), self._post_close_market_summary(item["symbol"]))
                entries.append({"ok": False, "item": item, "error": str(exc), "message": message})
                detail_messages.append(message)
        overview = _format_post_close_overview(market_overview, entries)
        message = "\n\n".join([overview, *detail_messages])
        self._record_review_result("success" if any(entry.get("ok") for entry in entries) else "failed", message, attempted_at, today)
        return message

    def _post_close_review_item(self, item: dict[str, Any]) -> dict[str, Any]:
        symbol = normalize_symbol(item["symbol"])
        name = str(item.get("name") or symbol)
        klines = self._latest_rows("klines_daily", symbol, "trade_date", 160)
        if not klines:
            klines = self.fetch_klines(symbol, 160)
        if not klines:
            raise RuntimeError("缺少日K数据，无法收盘复盘。")
        trade_date = str(klines[-1].get("trade_date") or today_text())
        validation = self._post_close_validation(symbol, trade_date)
        composite_text = self.analyze(symbol)
        level_row = next((row for row in self.store.rows("key_levels") if row.get("symbol") == symbol), None)
        levels = _levels_from_row(level_row) or _extract_levels(composite_text) or _fallback_levels(safe_float(klines[-1].get("close"), 0.0) or 0.0, item.get("costPrice"))
        market_summary = self._post_close_market_summary(symbol)
        flash_context = self._post_close_flash_context(symbol, trade_date)
        peer_context = self._post_close_peer_context(item)
        review = self._post_close_review_decision(
            item=item,
            validation=validation,
            composite_text=composite_text,
            levels=levels,
            market_summary=market_summary,
            flash_context=flash_context,
            peer_context=peer_context,
        )
        message = _format_post_close_detail_message(item, validation, review, market_summary, peer_context)
        self._persist_post_close_review(symbol, message, review)
        return {"ok": True, "item": item, "validation": validation, "review": review, "message": message}

    def _persist_post_close_review(self, symbol: str, message: str, review: dict[str, Any]) -> None:
        decision = str(review.get("decision") or "recompute")
        levels = review.get("levels") if isinstance(review.get("levels"), dict) else None
        if decision == "invalidate" or not levels:
            try:
                self.store.open("key_levels").delete(f"symbol = '{symbol}'")
            except Exception:
                pass
            return
        row = _level_row(symbol, message, levels)
        self.store.replace_where("key_levels", f"symbol = '{symbol}'", [row])
        self.store.replace_where(
            "key_levels_history",
            f"symbol = '{symbol}' AND analysis_date = '{today_text()}' AND profile = 'composite'",
            [{**row, "activated_at": now_text(), "profile": "composite"}],
        )

    def _post_close_validation(self, symbol: str, trade_date: str) -> dict[str, Any]:
        snapshots = [
            row for row in self.store.rows("key_levels_history")
            if row.get("symbol") == symbol and str(row.get("analysis_date") or "") < trade_date
        ]
        snapshots = sorted(snapshots, key=lambda row: str(row.get("analysis_date") or ""), reverse=True)
        snapshot = snapshots[0] if snapshots else None
        if not snapshot:
            return {
                "available": False,
                "snapshotDate": None,
                "evaluatedTradeDate": trade_date,
                "verdict": "unavailable",
                "snapshot": None,
                "summary": "昨日无可验证的活动关键位快照，本轮只能基于今日数据直接重算。",
                "lines": ["暂无昨日活动关键位快照。"],
            }
        daily_rows = sorted([row for row in self.store.rows("klines_daily") if row.get("symbol") == symbol], key=lambda row: str(row.get("trade_date") or ""))
        row = next((item for item in daily_rows if item.get("trade_date") == trade_date), None)
        if row is None:
            row = next((item for item in daily_rows if str(item.get("trade_date") or "") > str(snapshot.get("analysis_date") or "")), None)
        if row is None:
            return {
                "available": False,
                "snapshotDate": snapshot.get("analysis_date"),
                "evaluatedTradeDate": None,
                "verdict": "unavailable",
                "snapshot": snapshot,
                "summary": f"已找到 {snapshot.get('analysis_date')} 的关键位快照，但尚无后续交易日数据可供验证。",
                "lines": ["缺少后续交易日数据。"],
            }
        intraday_rows = [
            item for item in self.store.rows("klines_intraday")
            if item.get("symbol") == symbol and item.get("period") == "1m" and item.get("trade_date") == row.get("trade_date")
        ]
        support = _evaluate_support(snapshot, row)
        resistance = _evaluate_resistance(snapshot, row)
        stop_loss = _evaluate_stop_loss(snapshot, row)
        take_profit = _evaluate_take_profit(snapshot, row)
        breakthrough = _evaluate_breakthrough(snapshot, row)
        path = _evaluate_path(snapshot, row, intraday_rows)
        verdict = _derive_validation_verdict(support, stop_loss, take_profit, breakthrough, path)
        lines = [
            f"快照日期 {snapshot.get('analysis_date')}，验证交易日 {row.get('trade_date')}。",
            f"当日K线: 高 {fmt_price(row.get('high'))} | 低 {fmt_price(row.get('low'))} | 收 {fmt_price(row.get('close'))}",
            support,
            resistance,
            stop_loss,
            take_profit,
            breakthrough,
            path,
        ]
        return {
            "available": True,
            "snapshotDate": snapshot.get("analysis_date"),
            "evaluatedTradeDate": row.get("trade_date"),
            "verdict": verdict,
            "snapshot": snapshot,
            "summary": f"昨日关键位{_validation_label(verdict)}。",
            "lines": lines,
        }

    def _post_close_market_summary(self, symbol: str) -> dict[str, Any] | None:
        klines = self._latest_rows("klines_daily", symbol, "trade_date", 5)
        quote = None
        try:
            quote = (self.tickflow.quotes([symbol]) or [None])[0]
        except Exception:
            quote = None
        latest = klines[-1] if klines else {}
        latest_close = safe_float(latest.get("close"))
        if latest_close is None:
            latest_close = _quote_price(quote or {})
        change_pct = None
        if latest:
            prev_close = safe_float(latest.get("prev_close"))
            close = safe_float(latest.get("close"))
            if prev_close and close is not None:
                change_pct = (close - prev_close) / abs(prev_close) * 100
        if change_pct is None and quote:
            change_pct = _quote_change_pct(quote)
        return {"latestClose": latest_close, "dailyChangePct": change_pct} if latest_close is not None or change_pct is not None else None

    def _post_close_market_overview(self, date_prefix: str) -> str | None:
        rows = [
            row for row in self.store.rows("jin10_flash")
            if str(row.get("published_at") or "").startswith(date_prefix)
            and any(keyword in str(row.get("content") or "") for keyword in MARKET_OVERVIEW_FLASH_KEYWORDS)
        ]
        rows = sorted(rows, key=lambda row: str(row.get("published_at") or ""), reverse=True)
        if not rows:
            return self._post_close_index_overview()
        return "\n".join(f"• [{str(row.get('published_at') or '')[11:16]}] {_truncate(str(row.get('content') or ''), 120)}" for row in rows[:3])

    def _post_close_index_overview(self) -> str | None:
        lines = []
        for item in DEFAULT_MARKET_INDEXES:
            summary = self._post_close_market_summary(item["symbol"])
            latest_close = safe_float((summary or {}).get("latestClose"))
            change_pct = safe_float((summary or {}).get("dailyChangePct"))
            if latest_close is None and change_pct is None:
                continue
            parts = [f"• {item['name']}（{item['symbol']}）"]
            if latest_close is not None:
                parts.append(f"收 {latest_close:.2f}")
            if change_pct is not None:
                parts.append(f"当日 {change_pct:+.2f}%")
            lines.append("，".join(parts))
        return "\n".join(lines) if lines else None

    def _post_close_flash_context(self, symbol: str, date_prefix: str) -> dict[str, list[dict[str, Any]]]:
        deliveries = [
            row for row in self.store.rows("jin10_flash_delivery")
            if str(row.get("published_at") or "").startswith(date_prefix)
            and symbol in str(row.get("symbols_json") or "")
        ]
        overview = [
            row for row in self.store.rows("jin10_flash")
            if str(row.get("published_at") or "").startswith(date_prefix)
            and any(keyword in str(row.get("content") or "") for keyword in MARKET_OVERVIEW_FLASH_KEYWORDS)
        ]
        deliveries = sorted(deliveries, key=lambda row: str(row.get("published_at") or ""), reverse=True)[:5]
        overview = sorted(overview, key=lambda row: str(row.get("published_at") or ""), reverse=True)[:5]
        return {"stockAlerts": deliveries, "marketOverviewFlashes": overview}

    def _post_close_peer_context(self, item: dict[str, Any]) -> dict[str, Any]:
        sector = _clean_profile_text(item.get("sector"))
        if not sector:
            return {"available": False, "summary": "未记录申万行业分类。"}
        return {"available": False, "summary": f"行业: {sector}。Hermes Python 版暂未启用同业涨跌排名。"}

    def _post_close_review_decision(
        self,
        *,
        item: dict[str, Any],
        validation: dict[str, Any],
        composite_text: str,
        levels: dict[str, Any],
        market_summary: dict[str, Any] | None,
        flash_context: dict[str, list[dict[str, Any]]],
        peer_context: dict[str, Any],
    ) -> dict[str, Any]:
        fallback = _fallback_post_close_review(validation, composite_text, levels, market_summary, flash_context, peer_context)
        if self.config.llm_base_url and self.config.llm_api_key and self.config.llm_model:
            try:
                prompt = _build_post_close_review_prompt(item, validation, composite_text, levels, market_summary, flash_context, peer_context)
                text = call_llm(self.config, POST_CLOSE_REVIEW_SYSTEM, prompt, max_tokens=1800, temperature=0.2)
                parsed = _extract_json_object(text or "")
                if parsed:
                    return _normalize_post_close_review(parsed, fallback)
            except Exception:
                pass
        return fallback

    def analyze(self, symbol: str) -> str:
        symbol = normalize_symbol(symbol)
        watch = next((row for row in self.watchlist() if row["symbol"] == symbol), None)
        klines = self._latest_rows("klines_daily", symbol, "trade_date", 120)
        if not klines:
            klines = self.fetch_klines(symbol, 120)
            from .indicators import calculate_indicators

            self.store.replace_where("indicators", f"symbol = '{symbol}'", calculate_indicators(klines))
        indicators = self._latest_rows("indicators", symbol, "trade_date", 40)
        quote = (self.tickflow.quotes([symbol]) or [{}])[0]
        latest_price = _quote_price(quote) or safe_float(klines[-1].get("close") if klines else None, 0.0) or 0.0
        news_docs: list[dict[str, Any]] = []
        try:
            news_docs = self.mx.search(f"{watch.get('name') if watch else symbol} 最新 公告 研报 资讯")[:5]
        except Exception:
            news_docs = []
        financial: Any = None
        if supports_financial(self.config.tickflow_api_key_level):
            try:
                financial = self.tickflow.financial_snapshot(symbol, latest=3)
            except Exception:
                financial = None
        user = self._analysis_user_prompt(symbol, watch, klines[-40:], indicators[-20:], quote, financial, news_docs)
        text = call_llm(self.config, ANALYSIS_SYSTEM, user)
        levels = _extract_levels(text)
        if not levels:
            levels = _fallback_levels(latest_price, watch.get("costPrice") if watch else None)
        row = _level_row(symbol, text, levels)
        self.store.replace_where("key_levels", f"symbol = '{symbol}'", [row])
        self.store.add("key_levels_history", [{**row, "activated_at": now_text(), "profile": "composite"}])
        self.store.add("analysis_log", [{"symbol": symbol, "analysis_date": today_text(), "analysis_text": text, "structured_ok": 1 if levels else 0}])
        self.store.add("technical_analysis", [{**{k: row.get(k) for k, _, _ in SCHEMAS["technical_analysis"]}, "symbol": symbol, "analysis_date": today_text(), "analysis_text": text, "structured_ok": 1}])
        self.store.add("financial_analysis", [{"symbol": symbol, "analysis_date": today_text(), "analysis_text": "见综合分析", "score": None, "bias": "neutral", "strengths_json": "[]", "risks_json": "[]", "watch_items_json": "[]", "evidence_json": json_text({"available": financial is not None})}])
        self.store.add("news_analysis", [{"symbol": symbol, "analysis_date": today_text(), "query": "latest", "analysis_text": "见综合分析", "score": None, "bias": "neutral", "catalysts_json": "[]", "risks_json": "[]", "watch_items_json": "[]", "source_count": len(news_docs), "evidence_json": json_text({"documents": news_docs[:5]})}])
        self.store.add("composite_analysis", [{**{k: row.get(k) for k, _, _ in SCHEMAS["composite_analysis"]}, "symbol": symbol, "analysis_date": today_text(), "analysis_text": text, "structured_ok": 1, "technical_score": row.get("score"), "financial_score": None, "news_score": None, "financial_bias": "neutral", "news_bias": "neutral", "evidence_json": json_text({"news_source_count": len(news_docs), "financial_available": financial is not None})}])
        return text

    def view_analysis(self, symbol: str, profile: str = "composite", limit: int = 1) -> str:
        symbol = normalize_symbol(symbol)
        table_map = {"technical": "technical_analysis", "financial": "financial_analysis", "news": "news_analysis", "all": "analysis_log", "composite": "composite_analysis"}
        table = table_map.get(profile, "composite_analysis")
        rows = self._latest_rows(table, symbol, "analysis_date", limit)
        if not rows:
            return f"未找到 {symbol} 的{profile}分析记录。"
        parts = []
        for row in reversed(rows):
            parts.append(f"## {row.get('analysis_date')} {profile}\n{row.get('analysis_text')}")
        return "\n\n".join(parts)

    def backtest_key_levels(self, symbol: str | None = None, recent_limit: int = 20) -> str:
        rows = self.store.rows("key_levels_history")
        if symbol:
            norm = normalize_symbol(symbol)
            rows = [row for row in rows if row.get("symbol") == norm]
        rows = sorted(rows, key=lambda r: str(r.get("activated_at") or r.get("analysis_date") or ""), reverse=True)[:recent_limit]
        if not rows:
            return "暂无可回测的关键价位历史。"
        lines = [f"📍 活动关键价位回看（{len(rows)}条）"]
        for row in rows:
            lines.append(f"• {row.get('symbol')} {row.get('analysis_date')} current={fmt_price(row.get('current_price'))} support={fmt_price(row.get('support'))} resistance={fmt_price(row.get('resistance'))} stop_loss={fmt_price(row.get('stop_loss'))} score={row.get('score')}")
        return "\n".join(lines)

    def mx_search_text(self, query: str) -> str:
        docs = self.mx.search(query)
        if not docs:
            return "未搜索到相关结果。"
        lines = [f"🔎 妙想资讯搜索: {query}"]
        for idx, doc in enumerate(docs[:10], 1):
            lines.append(f"{idx}. {doc.get('title')}\n{doc.get('trunk')}\n来源: {doc.get('source') or '-'} 时间: {doc.get('publishedAt') or '-'}")
        return "\n\n".join(lines)

    def mx_data_text(self, query: str) -> str:
        return json.dumps(self.mx.data(query), ensure_ascii=False, indent=2)[:12000]

    def mx_select_text(self, keyword: str, limit: int = 20) -> str:
        result = self.mx.select(keyword, page_size=limit)
        return _render_mx_select(keyword, result, limit)

    def screen_candidates(self, keyword: str, limit: int = 3, summarize: bool = False) -> str:
        result = self.mx.select(keyword, page_size=max(limit, 20))
        candidates = _extract_candidates(result, limit)
        if not candidates:
            return f"🧭 智能选股候选池: {keyword}\n未解析到候选股票。"
        quotes = _quote_map(self.tickflow.quotes([c["symbol"] for c in candidates]))
        lines = [f"🧭 智能选股候选池: {keyword}", f"候选数: {len(candidates)}"]
        for idx, c in enumerate(candidates, 1):
            q = quotes.get(c["symbol"], {})
            latest_price = _quote_price(q)
            if latest_price is None:
                latest_price = safe_float(c.get("latestPrice"))
            change_pct = _quote_change_pct(q)
            if change_pct is None:
                change_pct = safe_float(c.get("changePct"))
            lines.append(f"{idx}. {c['name']}（{c['symbol']}） 现价: {fmt_price(latest_price)} 涨跌幅: {pct(change_pct)}")
            try:
                kl = self.fetch_klines(c["symbol"], count=20, persist=False)
                if kl:
                    lines.append(f"   日K: {kl[0]['trade_date']}~{kl[-1]['trade_date']} 最新收盘 {kl[-1]['close']:.2f}")
            except Exception as exc:
                lines.append(f"   日K获取失败: {exc}")
        text = "\n".join(lines)
        if summarize:
            summary = call_llm(self.config, "你是A股候选池整理助手，只能基于输入文本总结。", text, max_tokens=900, temperature=0.2)
            return f"{text}\n\nLLM整理:\n{summary}"
        return text

    def eastmoney_watchlist(self) -> str:
        return json.dumps(self.mx.eastmoney_watchlist(), ensure_ascii=False, indent=2)[:12000]

    def sync_eastmoney_watchlist(self) -> str:
        result = self.mx.eastmoney_watchlist()
        stocks = _extract_eastmoney_stocks(result)
        if not stocks:
            return "未从东方财富自选中解析到股票。"
        count = 0
        for stock in stocks:
            try:
                symbol = normalize_symbol(stock.get("code") or stock.get("symbol") or "")
                row = {"symbol": symbol, "name": stock.get("name") or symbol, "costPrice": 0, "addedAt": now_text(), "sector": None, "themes": None, "themeQuery": None, "themeUpdatedAt": None}
                self.store.replace_where("watchlist", f"symbol = '{symbol}'", [row])
                count += 1
            except Exception:
                continue
        return f"✅ 已从东方财富同步自选到本地: {count} 只"

    def push_eastmoney_watchlist(self) -> str:
        rows = self.watchlist()
        if not rows:
            return "本地自选列表为空。"
        ok, failed = 0, []
        for row in rows:
            try:
                self.mx.manage_watchlist(f"添加 {symbol_code(row['symbol'])}")
                ok += 1
            except Exception as exc:
                failed.append(f"{row['symbol']}: {exc}")
        return "\n".join([f"✅ 已推送到东方财富自选: {ok} 只", *failed[:10]])

    def remove_eastmoney_watchlist(self, symbol: str) -> str:
        code = symbol_code(symbol)
        result = self.mx.manage_watchlist(f"删除 {code}")
        return f"✅ 已请求删除东方财富自选 {code}\n{json.dumps(result, ensure_ascii=False)[:2000]}"

    def query_database(self, action: str = "tables", table: str | None = None, symbol: str | None = None, limit: int = 10, fields: list[str] | None = None, sort_by: str | None = None, sort_order: str = "desc", contains: str | None = None) -> str:
        if action == "tables":
            names = self.store.table_names()
            return "📚 LanceDB 数据表:\n" + ("\n".join(f"• {name}" for name in names) if names else "暂无数据表")
        if not table:
            raise ValueError("query_database requires table")
        table = _table_alias(table)
        if action == "schema":
            rows = self.store.schema_description(table)
            return f"🧬 {table} 字段:\n" + "\n".join(f"• {r['name']} {r['type']} nullable={r['nullable']}" for r in rows)
        rows = self.store.rows(table)
        if symbol:
            rows = [r for r in rows if r.get("symbol") == normalize_symbol(symbol) or r.get("symbol") == symbol]
        if contains:
            needle = contains.lower()
            rows = [r for r in rows if needle in json.dumps(r, ensure_ascii=False).lower()]
        if sort_by:
            rows = sorted(rows, key=lambda r: str(r.get(sort_by) or ""), reverse=sort_order != "asc")
        rows = rows[:limit]
        if fields:
            rows = [{k: row.get(k) for k in fields} for row in rows]
        return f"🔎 {table} 查询结果（{len(rows)}条）\n" + json.dumps(rows, ensure_ascii=False, indent=2)

    def start_monitor(self) -> str:
        if not self.watchlist():
            return "⚠️ 无法启动实时监控\n原因: 关注列表为空，请先添加至少一只自选股。"
        state = self._read_state("monitor-state.json")
        state.update({"running": True, "startedAt": state.get("startedAt") or now_text(), "lastStoppedAt": None, "runtimeHost": "hermes_thread", "runtimeObservedAt": now_text(), "lastLoopError": None, "lastLoopErrorAt": None})
        self._write_state("monitor-state.json", state)
        if not self.monitor_thread or not self.monitor_thread.is_alive():
            self.monitor_stop.clear()
            self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self.monitor_thread.start()
        return f"✅ TickFlow 实时监控已启动\n运行方式: hermes_thread\n轮询间隔: {self.config.request_interval} 秒"

    def stop_monitor(self) -> str:
        self.monitor_stop.set()
        state = self._read_state("monitor-state.json")
        state.update({"running": False, "lastStoppedAt": now_text()})
        self._write_state("monitor-state.json", state)
        return "🛑 TickFlow 实时监控已停止"

    def ensure_monitor_running(self) -> bool:
        state = self._read_state("monitor-state.json")
        if not state.get("running"):
            return False
        if self.monitor_thread and self.monitor_thread.is_alive():
            return False
        if not self.watchlist():
            return False
        self.start_monitor()
        return True

    def monitor_status(self) -> str:
        auto_recovered = self.ensure_monitor_running()
        state = self._read_state("monitor-state.json")
        thread_alive = bool(self.monitor_thread and self.monitor_thread.is_alive())
        stale = _state_heartbeat_stale(state, self.config.request_interval, MONITOR_STALE_GRACE_SECONDS)
        watch_rows = self.watchlist()
        level_symbols = {row.get("symbol") for row in self.store.rows("key_levels") if row.get("symbol")}
        level_count = len([row for row in watch_rows if row.get("symbol") in level_symbols])
        if state.get("running") and thread_alive and not stale:
            status = "✅ 运行中"
        elif state.get("running"):
            status = "⚠️ 已启用但后台未正常心跳"
        else:
            status = "⭕ 未启动"
        lines = [
            "📊 监控状态",
            f"状态: {status}",
            "运行方式: hermes_thread",
            f"轮询间隔: {self.config.request_interval} 秒",
            f"后台线程: {'存活' if thread_alive else '未运行'}",
            f"最近心跳: {_format_heartbeat(state.get('lastHeartbeatAt'), self.config.request_interval, MONITOR_STALE_GRACE_SECONDS) or '暂无'}",
            f"可用关键位: {level_count}/{len(watch_rows)} 只",
        ]
        if auto_recovered:
            lines.append("自动恢复: 已重新启动实时监控线程")
        if state.get("lastMonitorCheckAt"):
            lines.append(
                f"最近轮询: {state.get('lastMonitorCheckAt')} | 报价 {safe_int(state.get('lastQuoteCount'), 0) or 0} | "
                f"关键位 {safe_int(state.get('lastKeyLevelCount'), 0) or 0} | 股价告警 {safe_int(state.get('lastPriceAlertCount'), 0) or 0}"
            )
        if state.get("lastPriceAlertError"):
            lines.append(f"最近股价告警异常: {state.get('lastPriceAlertErrorAt') or '未知时间'} | {state.get('lastPriceAlertError')}")
        if state.get("lastLoopError"):
            lines.append(f"最近异常: {state.get('lastLoopErrorAt') or '未知时间'} | {state.get('lastLoopError')}")
        if state.get("lastSessionNotificationError"):
            lines.append(f"最近阶段提醒异常: {state.get('lastSessionNotificationErrorAt') or '未知时间'} | {state.get('lastSessionNotificationError')}")
        if state.get("lastSessionNotificationSentAt"):
            lines.append(f"最近阶段提醒: {state.get('lastSessionNotificationSentAt')} | {state.get('lastSessionNotificationId') or '-'}")
        lines.append(self.list_watchlist())
        return "\n".join(lines)

    def start_daily_update(self) -> str:
        state = self._read_daily_state()
        old_job_ids = list(state.get("jobIds") or [])
        started_at = now_text()
        state.update({"running": True, "scheduleVersion": DAILY_SCHEDULE_VERSION, "startedAt": state.get("startedAt") or started_at, "lastStoppedAt": None, "runtimeHost": "hermes_thread", "runtimeObservedAt": started_at, "lastHeartbeatAt": started_at, "jobIds": [], "lastError": None, "lastErrorAt": None, "disabledByUser": False})
        self._write_daily_state(state)
        if not self.daily_thread or not self.daily_thread.is_alive():
            self.daily_stop.clear()
            self.daily_thread = threading.Thread(target=self._daily_update_loop, daemon=True)
            self.daily_thread.start()
        lines = ["✅ TickFlow 定时任务已启动", "运行方式: hermes_thread", f"盘前资讯: 交易日 {PRE_MARKET_BRIEF_READY_TIME}（窗口至 {PRE_MARKET_BRIEF_EXPIRE_TIME}）", f"日更: 交易日 {DAILY_UPDATE_READY_TIME}", f"复盘: 交易日 {POST_CLOSE_REVIEW_READY_TIME}", f"轮询间隔: {DAILY_UPDATE_LOOP_INTERVAL_SECONDS} 秒"]
        if old_job_ids:
            lines.append(f"已忽略旧 Hermes cron 任务记录: {', '.join(str(item) for item in old_job_ids)}")
        return "\n".join(lines)

    def stop_daily_update(self) -> str:
        self.daily_stop.set()
        state = self._read_daily_state()
        state.update({"running": False, "lastStoppedAt": now_text(), "disabledByUser": True})
        state.pop("jobIds", None)
        self._write_daily_state(state)
        return "🛑 TickFlow 定时任务已停止"

    def should_autostart_daily_update(self) -> bool:
        state = self._read_daily_state()
        return bool(state.get("running")) or not bool(state.get("disabledByUser"))

    def ensure_daily_update_running(self) -> bool:
        if not self.should_autostart_daily_update():
            return False
        if self.daily_thread and self.daily_thread.is_alive():
            return False
        self.start_daily_update()
        return True

    def daily_update_status(self) -> str:
        auto_recovered = self.ensure_daily_update_running()
        state = self._read_daily_state()
        today = today_text()
        thread_alive = bool(self.daily_thread and self.daily_thread.is_alive())
        stale = _state_heartbeat_stale(state, DAILY_UPDATE_LOOP_INTERVAL_SECONDS, DAILY_UPDATE_STALE_GRACE_SECONDS)
        if state.get("running") and thread_alive and not stale:
            status = "✅ 运行中"
        elif state.get("running"):
            status = "⚠️ 已启用但后台未正常心跳"
        elif state.get("disabledByUser"):
            status = "⭕ 已手动停用"
        else:
            status = "⭕ 未启动"
        lines = [
            "🕒 盘前资讯 / 定时日更 / 收盘复盘状态",
            f"状态: {status}",
            "运行方式: hermes_thread",
            "配置来源: Hermes plugin/env/local.config.json",
            f"调度: 盘前资讯 {PRE_MARKET_BRIEF_READY_TIME}（窗口至 {PRE_MARKET_BRIEF_EXPIRE_TIME}） | 日更 {DAILY_UPDATE_READY_TIME} | 复盘 {POST_CLOSE_REVIEW_READY_TIME} | 交易日周一至周五",
            f"轮询间隔: {DAILY_UPDATE_LOOP_INTERVAL_SECONDS} 秒",
            f"后台线程: {'存活' if thread_alive else '未运行'}",
            f"最近心跳: {_format_heartbeat(state.get('lastHeartbeatAt'), DAILY_UPDATE_LOOP_INTERVAL_SECONDS, DAILY_UPDATE_STALE_GRACE_SECONDS) or '暂无'}",
        ]
        if auto_recovered:
            lines.append("自动恢复: 已重新启动定时日更线程")
        lines.extend([
            "",
            "盘前资讯:",
            f"• 今日已生成: {'是' if state.get('lastPreMarketSuccessDate') == today else '否'}",
            f"• 最近尝试: {state.get('lastPreMarketAttemptAt') or '暂无'}",
            f"• 最近成功: {state.get('lastPreMarketSuccessAt') or '暂无'}",
            f"• 最近结果: {_format_task_result(state.get('lastPreMarketResultType'))}",
            "",
            "日更执行:",
            f"• 今日已更新: {'是' if state.get('lastSuccessDate') == today else '否'}",
            f"• 最近尝试: {state.get('lastAttemptAt') or '暂无'}",
            f"• 最近成功: {state.get('lastSuccessAt') or '暂无'}",
            f"• 最近结果: {_format_task_result(state.get('lastResultType'))}",
            "",
            "复盘执行:",
            f"• 今日已复盘: {'是' if state.get('lastReviewSuccessDate') == today else '否'}",
            f"• 最近尝试: {state.get('lastReviewAttemptAt') or '暂无'}",
            f"• 最近成功: {state.get('lastReviewSuccessAt') or '暂无'}",
            f"• 最近结果: {_format_task_result(state.get('lastReviewResultType'))}",
        ])
        for count_key, summary_key, title in [
            ("preMarketConsecutiveFailures", "lastPreMarketResultSummary", "盘前资讯"),
            ("consecutiveFailures", "lastResultSummary", "日更执行"),
            ("reviewConsecutiveFailures", "lastReviewResultSummary", "复盘执行"),
        ]:
            failures = safe_int(state.get(count_key), 0) or 0
            summary = state.get(summary_key)
            if failures:
                lines.append(f"{title}连续失败: {failures}")
            if summary:
                lines.append(f"{title}最近摘要: {summary}")
        if state.get("lastError"):
            lines.append(f"最近调度异常: {state.get('lastErrorAt') or '未知时间'} | {state.get('lastError')}")
        if state.get("lastNotificationSentAt"):
            lines.append(f"最近投递成功: {state.get('lastNotificationSentAt')} | {state.get('lastNotificationTarget') or '-'}")
        if state.get("lastNotificationError"):
            lines.append(f"最近投递异常: {state.get('lastNotificationErrorAt') or '未知时间'} | {state.get('lastNotificationError')}")
        return "\n".join(lines)

    def flash_monitor_status(self) -> str:
        state = self._read_flash_state()
        rows = self.store.rows("jin10_flash")
        latest = max(rows, key=lambda r: int(r.get("published_ts") or 0), default=None)
        day_start = f"{today_text()} 00:00:00"
        day_start_ts = int((_parse_china_timestamp(day_start) or 0) * 1000)
        stored_today = len([row for row in rows if int(row.get("published_ts") or 0) >= day_start_ts])
        alerts_today = len([row for row in self.store.rows("jin10_flash_delivery") if str(row.get("delivered_at") or "") >= day_start])
        watchlist_count = len(self.watchlist())
        config_error = "" if self.jin10.configured() else "Jin10 MCP 未配置，请设置 jin10ApiToken"
        running = bool(self.flash_thread and self.flash_thread.is_alive())
        status = f"未配置（{config_error}）" if config_error else ("后台轮询中" if running else "未启动")
        lines = [
            "📰 Jin10 快讯监控状态",
            f"状态: {status}",
            f"轮询间隔: {self.config.jin10_flash_poll_interval} 秒",
            f"保留天数: {self.config.jin10_flash_retention_days} 天",
            f"关注列表: {watchlist_count}只",
            f"最近心跳: {state.get('lastHeartbeatAt') or '暂无'}",
            f"最近轮询: {state.get('lastPollAt') or '暂无'}",
            f"最近一轮: 入库 {safe_int(state.get('lastPollStored'), 0) or 0} 条 | 候选 {safe_int(state.get('lastPollCandidates'), 0) or 0} 条 | 告警 {safe_int(state.get('lastPollAlerts'), 0) or 0} 条",
            f"今日统计: 入库 {stored_today} 条 | 告警 {alerts_today} 条",
            f"续页补齐: {_format_flash_backfill_status(state)}",
            f"最近清理: {state.get('lastPrunedAt') or '暂无'}",
        ]
        if state.get("lastLoopError"):
            lines.append(f"最近异常: {state.get('lastLoopErrorAt') or '未知时间'} | {state.get('lastLoopError')}")
        if latest:
            lines.extend(["", "最新快讯:", f"• 时间: {latest.get('published_at')}", f"• 链接: {latest.get('url') or '-'}", f"• 正文: {_truncate(str(latest.get('content') or ''), 140)}"])
        return "\n".join(lines)

    def start_flash_monitor(self) -> str:
        if not self.jin10.configured():
            state = self._read_flash_state()
            state.update({"runtimeHost": "hermes_thread", "runtimeObservedAt": now_text(), "lastLoopError": "Jin10 MCP 未配置，请设置 jin10ApiToken", "lastLoopErrorAt": now_text()})
            self._write_flash_state(state)
            return "⚠️ Jin10 MCP 未配置，快讯后台轮询未启动。"
        if not self.flash_thread or not self.flash_thread.is_alive():
            self.flash_stop.clear()
            self.flash_thread = threading.Thread(target=self._flash_loop, daemon=True)
            self.flash_thread.start()
        return f"✅ Jin10 快讯后台轮询已启动\n轮询间隔: {self.config.jin10_flash_poll_interval} 秒"

    def _flash_loop(self) -> None:
        while not self.flash_stop.is_set():
            self._record_flash_heartbeat()
            try:
                self._flash_monitor_once()
            except Exception as exc:
                self._record_flash_error(exc)
            self.flash_stop.wait(self.config.jin10_flash_poll_interval)

    def _flash_monitor_once(self) -> int:
        now = now_text()
        now_ts = int(time.time() * 1000)
        state = self._read_flash_state()
        latest_stored = None if state.get("lastSeenKey") else self._latest_flash()
        anchor_key = state.get("lastSeenKey") or (latest_stored or {}).get("flash_key")
        anchor_published_at = state.get("lastSeenPublishedAt") or (latest_stored or {}).get("published_at")
        anchor_url = state.get("lastSeenUrl") or (latest_stored or {}).get("url")

        if not self.jin10.configured():
            state.update({"initialized": state.get("initialized") or bool(anchor_key), "lastSeenKey": anchor_key, "lastSeenPublishedAt": anchor_published_at, "lastSeenUrl": anchor_url, "lastPollAt": now, "lastPollStored": 0, "lastBackfillStored": 0, "lastPollCandidates": 0, "lastPollAlerts": 0, "lastLoopError": None, "lastLoopErrorAt": None})
            self._write_flash_state(state)
            return 0

        if not anchor_key and not state.get("initialized"):
            seed = self._fetch_latest_flashes(FLASH_INITIAL_SEED_PAGES, None)
            save = self._save_flash_records(seed["items"])
            state.update({"initialized": True, "lastSeenKey": (seed.get("latest") or {}).get("flash_key"), "lastSeenPublishedAt": (seed.get("latest") or {}).get("published_at"), "lastSeenUrl": (seed.get("latest") or {}).get("url"), "backfillCursor": seed.get("nextCursor"), "lastPollAt": now, "lastPollStored": 0, "lastBackfillStored": save["added"], "lastPollCandidates": 0, "lastPollAlerts": 0, "lastLoopError": None, "lastLoopErrorAt": None})
            self._write_flash_state(state)
            self._maybe_prune_flash_records(state)
            return 0

        fetched = self._fetch_latest_flashes(FLASH_MAX_PAGES_PER_POLL, anchor_key)
        backfill_cursor = state.get("backfillCursor") or fetched.get("nextCursor")
        latest_save = self._save_flash_records(fetched["items"])
        backfill = self._fetch_flashes_by_cursor(FLASH_BACKFILL_PAGES_PER_POLL, backfill_cursor, self._flash_retention_cutoff_ts()) if backfill_cursor else None
        backfill_save = self._save_flash_records((backfill or {}).get("items") or [])
        new_keys = set(latest_save["addedKeys"])
        alertable = _filter_alertable_flash_records([item for item in fetched["items"] if item["flash_key"] in new_keys], state.get("lastPollAt"), now_ts, self.config.jin10_flash_poll_interval)
        candidates = _build_flash_candidates(alertable, self.watchlist())
        alerts = 0
        for candidate in candidates:
            alerts += self._handle_flash_candidate(candidate)
        next_backfill_cursor = backfill.get("nextCursor") if backfill is not None else backfill_cursor
        state.update({"initialized": True, "lastSeenKey": (fetched.get("latest") or {}).get("flash_key") or anchor_key, "lastSeenPublishedAt": (fetched.get("latest") or {}).get("published_at") or anchor_published_at, "lastSeenUrl": (fetched.get("latest") or {}).get("url") or anchor_url, "backfillCursor": next_backfill_cursor, "lastPollAt": now, "lastPollStored": latest_save["added"], "lastBackfillStored": backfill_save["added"], "lastPollCandidates": len(candidates), "lastPollAlerts": alerts, "lastLoopError": None, "lastLoopErrorAt": None})
        self._write_flash_state(state)
        self._maybe_prune_flash_records(state)
        return alerts

    def _fetch_latest_flashes(self, max_pages: int, anchor_key: str | None) -> dict[str, Any]:
        collected: list[dict[str, Any]] = []
        latest = None
        cursor = None
        for page_index in range(max_pages):
            page = self.jin10.list_flash(cursor)
            entries = [_to_flash_record(item) for item in _flash_page_items(page)]
            entries = [item for item in entries if item]
            if not entries:
                break
            latest = latest or entries[0]
            if anchor_key:
                anchor_index = next((idx for idx, item in enumerate(entries) if item["flash_key"] == anchor_key), -1)
                if anchor_index >= 0:
                    collected.extend(entries[:anchor_index])
                    return {"items": _sort_flash_records(collected), "latest": latest, "nextCursor": None}
            collected.extend(entries)
            next_cursor = _flash_next_cursor(page)
            if not _flash_has_more(page) or not next_cursor:
                return {"items": _sort_flash_records(collected), "latest": latest, "nextCursor": None}
            if page_index == max_pages - 1:
                return {"items": _sort_flash_records(collected), "latest": latest, "nextCursor": next_cursor}
            cursor = next_cursor
        return {"items": _sort_flash_records(collected), "latest": latest, "nextCursor": None}

    def _fetch_flashes_by_cursor(self, max_pages: int, initial_cursor: str, min_published_ts: int | None = None) -> dict[str, Any]:
        collected: list[dict[str, Any]] = []
        cursor = initial_cursor
        for page_index in range(max_pages):
            if not cursor:
                break
            page = self.jin10.list_flash(cursor)
            entries = [_to_flash_record(item) for item in _flash_page_items(page)]
            valid_entries = [item for item in entries if item]
            if min_published_ts is not None:
                collected.extend(item for item in valid_entries if int(item.get("published_ts") or 0) >= min_published_ts)
                if any(int(item.get("published_ts") or 0) < min_published_ts for item in valid_entries):
                    return {"items": _sort_flash_records(collected), "latest": None, "nextCursor": None}
            else:
                collected.extend(valid_entries)
            next_cursor = _flash_next_cursor(page)
            if not _flash_has_more(page) or not next_cursor:
                return {"items": _sort_flash_records(collected), "latest": None, "nextCursor": None}
            if page_index == max_pages - 1:
                return {"items": _sort_flash_records(collected), "latest": None, "nextCursor": next_cursor}
            cursor = next_cursor
        return {"items": _sort_flash_records(collected), "latest": None, "nextCursor": cursor}

    def _flash_retention_cutoff_ts(self) -> int:
        return int((time.time() - self.config.jin10_flash_retention_days * 24 * 60 * 60) * 1000)

    def _save_flash_records(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        existing = {row.get("flash_key") for row in self.store.rows("jin10_flash")}
        new_rows = [row for row in records if row.get("flash_key") and row.get("flash_key") not in existing]
        if new_rows:
            self.store.add("jin10_flash", new_rows)
        return {"added": len(new_rows), "addedKeys": [row["flash_key"] for row in new_rows]}

    def _handle_flash_candidate(self, candidate: dict[str, Any]) -> int:
        flash = candidate["flash"]
        if any(row.get("flash_key") == flash["flash_key"] for row in self.store.rows("jin10_flash_delivery")):
            return 0
        if not self.config.jin10_flash_night_alert and _is_night_quiet_hour():
            return 0
        decision = _fallback_flash_decision(candidate)
        if not decision["alert"]:
            return 0
        symbols = _resolve_flash_symbols(decision["relevantSymbols"], candidate["matches"])
        if not symbols:
            return 0
        message = _build_flash_alert_message(flash, candidate["matches"], decision, symbols)
        ok, _ = self.send_alert(message)
        if not ok:
            return 0
        self.store.add(
            "jin10_flash_delivery",
            [
                {
                    "flash_key": flash["flash_key"],
                    "published_at": flash["published_at"],
                    "symbols_json": json_text(symbols),
                    "headline": decision["headline"],
                    "reason": decision["reason"],
                    "importance": decision["importance"],
                    "message": message,
                    "delivered_at": now_text(),
                }
            ],
        )
        return 1

    def _latest_flash(self) -> dict[str, Any] | None:
        return max(self.store.rows("jin10_flash"), key=lambda r: int(r.get("published_ts") or 0), default=None)

    def _maybe_prune_flash_records(self, state: dict[str, Any]) -> None:
        last_pruned_ts = _parse_china_timestamp(state.get("lastPrunedAt")) or 0
        if time.time() - last_pruned_ts < FLASH_PRUNE_INTERVAL_SECONDS:
            return
        cutoff_ts = int((time.time() - self.config.jin10_flash_retention_days * 24 * 60 * 60) * 1000)
        cutoff_label = datetime.fromtimestamp(cutoff_ts / 1000, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        for table, predicate in [("jin10_flash", f"published_ts < {cutoff_ts}"), ("jin10_flash_delivery", f"delivered_at < '{cutoff_label}'")]:
            try:
                if self.store.has_table(table):
                    self.store.open(table).delete(predicate)
            except Exception:
                pass
        state["lastPrunedAt"] = now_text()
        self._write_flash_state(state)

    def _record_flash_heartbeat(self) -> None:
        state = self._read_flash_state()
        now = now_text()
        state.update({"lastHeartbeatAt": now, "runtimeHost": "hermes_thread", "runtimeObservedAt": now})
        self._write_flash_state(state)

    def _record_flash_error(self, error: Exception) -> None:
        state = self._read_flash_state()
        state.update({"lastLoopError": str(error), "lastLoopErrorAt": now_text()})
        self._write_flash_state(state)

    def _read_flash_state(self) -> dict[str, Any]:
        return {**FLASH_DEFAULT_STATE, **self._read_state("jin10-flash-monitor-state.json")}

    def _write_flash_state(self, state: dict[str, Any]) -> None:
        self._write_state("jin10-flash-monitor-state.json", {**FLASH_DEFAULT_STATE, **state})

    def test_alert(self) -> str:
        message = f"🧪 TickFlow 测试告警\n时间: {now_text()}\n说明: 这是一条由 Hermes 插件发出的测试消息。"
        media_path = None
        media_error = None
        if self.config.alert_image_enabled:
            try:
                media_path = self._write_alert_card(
                    title="TickFlow 测试告警",
                    label="测试",
                    name="平安银行",
                    symbol="000001.SZ",
                    current_price=12.36,
                    trigger_price=12.18,
                    cost_price=11.80,
                    note="用于验证 Hermes send_message 文本与 MEDIA PNG 投递链路。",
                    points=[("09:30", 12.02), ("10:00", 12.08), ("10:30", 12.12), ("11:30", 12.15), ("13:00", 12.19), ("13:30", 12.23), ("14:00", 12.27), ("14:12", 12.36)],
                    levels={"support": 12.08, "resistance": 12.30, "breakthrough": 12.18, "take_profit": 12.68, "stop_loss": 11.86},
                )
            except Exception as exc:
                media_error = str(exc)
                media_path = None
        ok, detail = self.send_alert(message, media_path=media_path)
        if ok and media_path:
            remove_alert_media(media_path)
            return "✅ 测试告警发送成功（文本 + PNG）"
        if ok and not self.config.alert_image_enabled:
            return "✅ 测试告警发送成功（文本；PNG 已关闭）"
        if ok and media_error:
            return f"✅ 测试告警发送成功（文本；PNG 生成失败：{media_error}）"
        if _unknown_tool(detail) and media_path:
            return (
                f"{message}\nMEDIA:{media_path}\n\n"
                "✅ 测试告警已生成（当前命令响应模式，文本 + PNG）。"
            )
        if _unknown_tool(detail):
            return f"{message}\n\n✅ 测试告警已生成（当前命令响应模式，文本）。"
        if media_path:
            remove_alert_media(media_path)
        return "✅ 测试告警发送成功（文本）" if ok else f"❌ 测试告警发送失败\n原因: {detail}"

    def send_alert(self, message: str, media_path: Path | None = None) -> tuple[bool, str | None]:
        try:
            target = self.config.alert_delivery_target
            if not target:
                return False, "请配置 alertDeliveryTarget，例如 telegram、telegram:CHAT_ID、telegram:CHAT_ID:THREAD_ID、discord:CHANNEL_ID。"
            if self.ctx is None:
                ok, detail = _send_direct_delivery(target, message, media_path)
                if ok:
                    return ok, detail
                return False, f"Hermes context unavailable；直投递失败: {detail}"
            content = message
            if media_path:
                content = f"{message}\nMEDIA:{media_path}"
            payload = {"action": "send", "target": target, "message": content}
            result = self.ctx.dispatch_tool("send_message", payload)
            parsed = json.loads(result) if isinstance(result, str) else result
            if isinstance(parsed, dict) and parsed.get("error"):
                detail = str(parsed.get("error"))
                if _unknown_tool(detail):
                    return _send_direct_delivery(target, message, media_path)
                return False, detail
            if isinstance(parsed, dict) and parsed.get("success") is False:
                detail = str(parsed)
                if _unknown_tool(detail):
                    return _send_direct_delivery(target, message, media_path)
                return False, detail
            return True, str(result)
        except Exception as exc:
            detail = str(exc)
            if _unknown_tool(detail):
                return _send_direct_delivery(self.config.alert_delivery_target, message, media_path)
            return False, detail

    def _monitor_loop(self) -> None:
        while not self.monitor_stop.is_set():
            state = self._read_state("monitor-state.json")
            if state.get("running"):
                state["lastHeartbeatAt"] = now_text()
                state["runtimeObservedAt"] = now_text()
                self._write_state("monitor-state.json", state)
                try:
                    self._monitor_once()
                    latest = self._read_state("monitor-state.json")
                    latest.update({"lastLoopError": None, "lastLoopErrorAt": None})
                    self._write_state("monitor-state.json", latest)
                except Exception as exc:
                    latest = self._read_state("monitor-state.json")
                    latest.update({"lastLoopError": str(exc), "lastLoopErrorAt": now_text()})
                    self._write_state("monitor-state.json", latest)
            self.monitor_stop.wait(self.config.request_interval)

    def _monitor_once(self) -> None:
        phase = self._monitor_phase()
        self._maybe_send_session_notification(phase)
        checked_at = now_text()
        if phase != "trading":
            state = self._read_state("monitor-state.json")
            state.update({"lastMonitorCheckAt": checked_at, "lastQuoteCount": 0, "lastKeyLevelCount": 0, "lastPriceAlertCount": 0})
            self._write_state("monitor-state.json", state)
            return
        rows = self.watchlist()
        quotes = _quote_map(self.tickflow.quotes([r["symbol"] for r in rows]))
        levels = {r.get("symbol"): r for r in self.store.rows("key_levels")}
        key_level_count = 0
        alert_count = 0
        last_alert_error = None
        for item in rows:
            quote = quotes.get(item["symbol"]) or {}
            level = levels.get(item["symbol"]) or {}
            if level:
                key_level_count += 1
            price = _quote_price(quote)
            if not price:
                continue
            change_pct = _quote_change_pct(quote)
            for field, op, label in _monitor_price_rules(level):
                target = safe_float(level.get(field))
                if target is None:
                    continue
                hit = price <= target if op == "<=" else price >= target
                key = hash_key(item["symbol"], field, today_text())
                if hit and not self._monitor_alert_sent(item["symbol"], key, _monitor_session_key()):
                    change_text = f"，涨跌幅 {change_pct:+.2f}%" if change_pct is not None else ""
                    message = f"【{label}】{item.get('name')}（{item['symbol']}）现价 {price:.2f}{change_text}，触发位 {target:.2f}"
                    media_path = None
                    if self.config.alert_image_enabled:
                        try:
                            media_path = self._write_alert_card(
                                title=f"TickFlow {label}",
                                label=label,
                                name=str(item.get("name") or item["symbol"]),
                                symbol=item["symbol"],
                                current_price=price,
                                trigger_price=target,
                                cost_price=safe_float(item.get("costPrice")),
                                note=message,
                                points=self._alert_points(item["symbol"], price),
                                levels={
                                    "stop_loss": safe_float(level.get("stop_loss")),
                                    "support": safe_float(level.get("support")),
                                    "resistance": safe_float(level.get("resistance")),
                                    "breakthrough": safe_float(level.get("breakthrough")),
                                    "take_profit": safe_float(level.get("take_profit")),
                                },
                            )
                        except Exception:
                            media_path = None
                    sent, detail = self._send_monitor_alert_result(item["symbol"], key, message, media_path=media_path)
                    if sent:
                        alert_count += 1
                    elif detail and detail != "duplicate":
                        last_alert_error = detail
        state = self._read_state("monitor-state.json")
        state.update({
            "lastMonitorCheckAt": checked_at,
            "lastQuoteCount": len(quotes),
            "lastKeyLevelCount": key_level_count,
            "lastPriceAlertCount": alert_count,
        })
        if last_alert_error:
            state.update({"lastPriceAlertError": last_alert_error, "lastPriceAlertErrorAt": now_text()})
        elif alert_count:
            state.update({"lastPriceAlertError": None, "lastPriceAlertErrorAt": None})
        self._write_state("monitor-state.json", state)

    def _monitor_phase(self) -> str:
        now = now_cn()
        today = now.strftime("%Y-%m-%d")
        if not self._is_trading_day(today):
            return "not_trading_day"
        hhmm = now.strftime("%H:%M")
        if hhmm < "09:30":
            return "pre_market"
        if hhmm <= "11:30":
            return "trading"
        if hhmm < "13:00":
            return "lunch_break"
        if hhmm <= "15:00":
            return "trading"
        return "closed"

    def _maybe_send_session_notification(self, phase: str) -> int:
        now = now_text()
        today = today_text()
        hhmm = now[11:16]
        state = self._read_state("monitor-state.json")
        previous_phase = state.get("lastObservedPhase") if state.get("lastObservedPhaseDate") == today else None
        sent = state.get("sessionNotificationsSent") if state.get("sessionNotificationsDate") == today else []
        sent = [str(item) for item in sent] if isinstance(sent, list) else []
        next_state = {
            **state,
            "lastObservedPhase": phase,
            "lastObservedPhaseDate": today,
            "sessionNotificationsDate": today,
            "sessionNotificationsSent": list(sent),
        }
        event = _resolve_monitor_session_notification(str(previous_phase) if previous_phase else None, phase, hhmm, sent)
        if not event:
            self._write_state("monitor-state.json", next_state)
            return 0

        message = _format_monitor_system_notification(
            event["title"],
            [
                f"时间: {now}",
                f"阶段: {event['phaseText']}",
                f"关注列表: {len(self.watchlist())}只",
            ],
        )
        sent_ok, detail = self._send_monitor_alert_result(SYSTEM_SESSION_ALERT_SYMBOL, event["id"], message)
        session_key = _monitor_session_key()
        if sent_ok or self._monitor_alert_sent(SYSTEM_SESSION_ALERT_SYMBOL, event["id"], session_key):
            if event["id"] not in next_state["sessionNotificationsSent"]:
                next_state["sessionNotificationsSent"].append(event["id"])
            next_state.update({
                "lastSessionNotificationId": event["id"],
                "lastSessionNotificationSentAt": now,
                "lastSessionNotificationError": None,
                "lastSessionNotificationErrorAt": None,
            })
        elif detail:
            next_state.update({
                "lastSessionNotificationId": event["id"],
                "lastSessionNotificationError": detail,
                "lastSessionNotificationErrorAt": now,
            })
        self._write_state("monitor-state.json", next_state)
        return 1 if sent_ok else 0

    def _send_monitor_alert(self, symbol: str, rule_name: str, message: str, media_path: Path | None = None) -> bool:
        ok, _ = self._send_monitor_alert_result(symbol, rule_name, message, media_path=media_path)
        return ok

    def _send_monitor_alert_result(self, symbol: str, rule_name: str, message: str, media_path: Path | None = None) -> tuple[bool, str | None]:
        session_key = _monitor_session_key()
        if self._monitor_alert_sent(symbol, rule_name, session_key):
            if media_path:
                remove_alert_media(media_path)
            return False, "duplicate"
        ok, detail = self.send_alert(message, media_path=media_path)
        if media_path:
            remove_alert_media(media_path)
        if not ok:
            return False, detail
        self.store.add("alert_log", [{"symbol": symbol, "alert_date": session_key, "rule_name": rule_name, "message": message, "triggered_at": now_text()}])
        return True, detail

    def _monitor_alert_sent(self, symbol: str, rule_name: str, session_key: str) -> bool:
        return any(
            row.get("symbol") == symbol
            and row.get("rule_name") == rule_name
            and row.get("alert_date") == session_key
            for row in self.store.rows("alert_log")
        )

    def _daily_update_loop(self) -> None:
        while not self.daily_stop.is_set():
            state = self._read_daily_state()
            if state.get("running"):
                now = now_text()
                state.update({"lastHeartbeatAt": now, "runtimeHost": "hermes_thread", "runtimeObservedAt": now})
                self._write_daily_state(state)
                try:
                    self._daily_update_once()
                except Exception as exc:
                    latest = self._read_daily_state()
                    latest.update({"lastError": str(exc), "lastErrorAt": now_text()})
                    self._write_daily_state(latest)
            self.daily_stop.wait(DAILY_UPDATE_LOOP_INTERVAL_SECONDS)

    def _daily_update_once(self) -> None:
        state = self._read_daily_state()
        today = today_text()
        hhmm = now_cn().strftime("%H:%M")
        if (
            PRE_MARKET_BRIEF_READY_TIME <= hhmm <= PRE_MARKET_BRIEF_EXPIRE_TIME
            and _should_run_scheduled_task(state, "lastPreMarketAttemptDate", "lastPreMarketSuccessDate", today)
        ):
            self._run_daily_scheduled_action(lambda: self.pre_market_brief(scheduled=True), "pre_market")
        elif hhmm > PRE_MARKET_BRIEF_EXPIRE_TIME and _should_run_scheduled_task(state, "lastPreMarketAttemptDate", "lastPreMarketSuccessDate", today):
            message = f"已超过盘前资讯窗口 {PRE_MARKET_BRIEF_READY_TIME}-{PRE_MARKET_BRIEF_EXPIRE_TIME}，今日不再补跑盘前资讯。"
            self._record_pre_market_result("skipped", message, now_text(), today)
        state = self._read_daily_state()
        if hhmm >= DAILY_UPDATE_READY_TIME and _should_run_scheduled_task(state, "lastAttemptDate", "lastSuccessDate", today):
            self._run_daily_scheduled_action(lambda: self.update_all(scheduled=True), "daily_update")
        state = self._read_daily_state()
        if hhmm >= POST_CLOSE_REVIEW_READY_TIME and _should_run_review_task(state, today):
            if state.get("lastSuccessDate") != today:
                message = f"今日日更尚未在 {DAILY_UPDATE_READY_TIME} 后成功完成，暂不执行收盘复盘"
                self._record_review_result("waiting_daily_update", message, now_text(), today)
            else:
                self._run_daily_scheduled_action(lambda: self.post_close_review(scheduled=True), "post_close_review")

    def _run_daily_scheduled_action(self, fn, kind: str = "generic") -> None:
        message = fn()
        if self.config.daily_update_notify and not str(message).startswith("[SILENT]"):
            attempted_at = now_text()
            ok, detail = self.send_alert(_format_scheduled_notification(kind, message))
            state = self._read_daily_state()
            state.update({
                "lastNotificationAttemptAt": attempted_at,
                "lastNotificationTarget": self.config.alert_delivery_target,
            })
            if ok:
                state.update({"lastNotificationSentAt": attempted_at, "lastNotificationError": None, "lastNotificationErrorAt": None})
            else:
                state.update({"lastNotificationError": detail or "send_alert failed", "lastNotificationErrorAt": attempted_at})
            self._write_daily_state(state)

    def _write_alert_card(
        self,
        *,
        title: str,
        label: str,
        name: str,
        symbol: str,
        current_price: float,
        trigger_price: float,
        cost_price: float | None = None,
        note: str,
        points: list[tuple[str, float]],
        levels: dict[str, float | None],
    ) -> Path:
        change_pct = None
        if len(points) >= 2 and points[0][1]:
            change_pct = ((points[-1][1] - points[0][1]) / abs(points[0][1])) * 100
        distance_pct = ((current_price - trigger_price) / abs(trigger_price)) * 100 if trigger_price else None
        profit_pct = ((current_price - cost_price) / abs(cost_price)) * 100 if cost_price else None
        return write_alert_card(
            self.config.database_path,
            AlertCardInput(
                title=title,
                label=label,
                name=name,
                symbol=symbol,
                current_price=current_price,
                trigger_price=trigger_price,
                note=note,
                points=points,
                levels=levels,
                cost_price=cost_price,
                change_pct=change_pct,
                distance_pct=distance_pct,
                profit_pct=profit_pct,
                timestamp_label=f"Hermes | {now_text()}",
            ),
        )

    def _alert_points(self, symbol: str, current_price: float) -> list[tuple[str, float]]:
        rows = self._latest_rows("klines_intraday", symbol, "trade_time", 8)
        points = [(str(row.get("trade_time") or "")[-5:], safe_float(row.get("close")) or current_price) for row in rows]
        if len(points) >= 2:
            return points
        daily = self._latest_rows("klines_daily", symbol, "trade_date", 8)
        points = [(str(row.get("trade_date") or "")[-5:], safe_float(row.get("close")) or current_price) for row in daily]
        if len(points) >= 2:
            return points
        return [("09:30", current_price), ("15:00", current_price)]

    def _sync_pre_market_flash_window(self, window: dict[str, Any]) -> None:
        cursor = None
        collected: list[dict[str, Any]] = []
        for _ in range(12):
            page = self.jin10.list_flash(cursor)
            records = [_to_flash_record(item) for item in _flash_page_items(page)]
            records = [item for item in records if item]
            if not records:
                break
            collected.extend(records)
            oldest_ts = min(int(item.get("published_ts") or 0) for item in records)
            next_cursor = _flash_next_cursor(page)
            if oldest_ts < int(window["startTs"]) or not _flash_has_more(page) or not next_cursor:
                break
            cursor = next_cursor
        self._save_flash_records(collected)

    def _build_pre_market_brief_text(self, window: dict[str, Any], watchlist: list[dict[str, Any]], flashes: list[dict[str, Any]], sync_warning: str | None = None) -> str:
        header = [
            f"🌅 开盘前资讯简报｜{str(window['endAt'])[:10]}",
            f"信息窗口: {window['startAt']} ~ {window['endAt']}",
            f"整理快讯: {len(flashes)} 条 | 自选: {len(watchlist)} 只 | 规则命中: {len(_matched_pre_market_symbols(flashes, watchlist))} 只",
            "",
        ]
        if sync_warning:
            header.extend([f"⚠️ {sync_warning}", ""])
        if not flashes:
            return "\n".join([*header, f"本窗口未检索到标题含“{PRE_MARKET_BRIEF_KEYWORD}”的快讯，今日无新增盘前整理摘要。"])
        prompt = _build_pre_market_prompt(window, watchlist, flashes)
        if self.config.llm_base_url and self.config.llm_api_key and self.config.llm_model:
            try:
                generated = call_llm(self.config, PRE_MARKET_BRIEF_SYSTEM, prompt, max_tokens=1600, temperature=0.2)
                if generated:
                    return "\n".join([*header, generated.strip()])
            except Exception:
                pass
        return "\n".join([*header, _fallback_pre_market_summary(flashes, watchlist)])

    def _record_pre_market_result(self, result_type: str, message: str, attempted_at: str, date: str) -> None:
        state = self._read_daily_state()
        state.update({
            "lastPreMarketAttemptAt": attempted_at,
            "lastPreMarketAttemptDate": date,
            "lastPreMarketResultType": result_type,
            "lastPreMarketResultSummary": _summarize_task_message(message),
            "preMarketConsecutiveFailures": (safe_int(state.get("preMarketConsecutiveFailures"), 0) or 0) + 1 if result_type == "failed" else 0,
        })
        if result_type == "success":
            state.update({"lastPreMarketSuccessAt": attempted_at, "lastPreMarketSuccessDate": date})
        self._write_daily_state(state)

    def _record_daily_update_result(self, result_type: str, message: str, attempted_at: str, date: str) -> None:
        state = self._read_daily_state()
        state.update({
            "lastAttemptAt": attempted_at,
            "lastAttemptDate": date,
            "lastResultType": result_type,
            "lastResultSummary": _summarize_task_message(message),
            "consecutiveFailures": (safe_int(state.get("consecutiveFailures"), 0) or 0) + 1 if result_type == "failed" else 0,
        })
        if result_type == "success":
            state.update({"lastSuccessAt": attempted_at, "lastSuccessDate": date})
        self._write_daily_state(state)

    def _record_review_result(self, result_type: str, message: str, attempted_at: str, date: str) -> None:
        state = self._read_daily_state()
        state.update({
            "lastReviewAttemptAt": attempted_at,
            "lastReviewAttemptDate": date,
            "lastReviewResultType": result_type,
            "lastReviewResultSummary": _summarize_task_message(message),
            "reviewConsecutiveFailures": (safe_int(state.get("reviewConsecutiveFailures"), 0) or 0) + 1 if result_type == "failed" else 0,
        })
        if result_type == "success":
            state.update({"lastReviewSuccessAt": attempted_at, "lastReviewSuccessDate": date})
        self._write_daily_state(state)

    def _read_daily_state(self) -> dict[str, Any]:
        return {**DAILY_DEFAULT_STATE, **self._read_state("daily-update-state.json")}

    def _write_daily_state(self, state: dict[str, Any]) -> None:
        self._write_state("daily-update-state.json", {**DAILY_DEFAULT_STATE, **state})

    def _is_trading_day(self, date_text: str) -> bool:
        try:
            if self._calendar_days is None:
                path = Path(self.config.calendar_file).expanduser()
                raw = path.read_text(encoding="utf-8")
                self._calendar_days = {line.strip() for line in raw.splitlines() if line.strip()}
            if self._calendar_days:
                return date_text in self._calendar_days
        except Exception:
            pass
        try:
            return datetime.fromisoformat(date_text).weekday() < 5
        except Exception:
            return now_cn().weekday() < 5

    def _instrument_name(self, symbol: str) -> str:
        try:
            inst = (self.tickflow.instruments([symbol]) or [{}])[0]
            return inst.get("name") or inst.get("display_name") or symbol
        except Exception:
            return symbol

    def _resolve_watchlist_profile(self, row: dict[str, Any]) -> dict[str, Any]:
        symbol = row["symbol"]
        name = str(row.get("name") or symbol)
        updated_at = now_text()
        industry_profile: dict[str, Any] = {}
        industry_error: Exception | None = None
        try:
            industry_profile = self._resolve_tickflow_industry_profile(symbol)
        except Exception as exc:
            industry_error = exc
        llm_profile = self._resolve_llm_theme_profile(symbol, name) if self._can_extract_profile_with_llm() else {}
        if not industry_profile and not llm_profile and industry_error:
            raise industry_error
        return {
            "sector": industry_profile.get("sector") or llm_profile.get("sector"),
            "themes": llm_profile.get("themes") or [],
            "themeQuery": _build_theme_query(name, symbol),
            "themeUpdatedAt": updated_at,
        }

    def _can_extract_profile_with_llm(self) -> bool:
        return bool(self.mx.configured() and self.config.llm_base_url and self.config.llm_api_key and self.config.llm_model)

    def _resolve_llm_theme_profile(self, symbol: str, name: str) -> dict[str, Any]:
        query = _build_theme_query(name, symbol)
        documents = self.mx.search(query)[:8]
        if not documents:
            return {}
        response = call_llm(
            self.config,
            WATCHLIST_PROFILE_EXTRACTION_SYSTEM,
            _build_profile_extraction_prompt(symbol, name, documents),
            max_tokens=1200,
            temperature=0.1,
        )
        parsed = _extract_json_object(response)
        if not parsed:
            return {}
        return {
            "sector": _clean_profile_text(parsed.get("sector")),
            "themes": _normalize_theme_labels(parsed.get("themes"), company_name=name),
        }

    def _resolve_tickflow_industry_profile(self, symbol: str) -> dict[str, Any]:
        catalog = self._ensure_universe_catalog()
        normalized_symbol = normalize_symbol(symbol)
        universe_ids = catalog["membership_ids_by_symbol"].get(normalized_symbol) or catalog["membership_ids_by_symbol"].get(symbol) or []
        if not universe_ids:
            return {}
        shenwan = []
        for universe_id in universe_ids:
            summary = catalog["summaries_by_id"].get(universe_id)
            parsed = _parse_shenwan_universe(summary) if summary else None
            if parsed:
                shenwan.append({"id": universe_id, **parsed})
        if not shenwan:
            return {}
        levels = {item["level"]: item for item in shenwan}
        names = [levels[level]["label"] for level in ["SW1", "SW2", "SW3"] if level in levels and levels[level].get("label")]
        return {
            "sector": "-".join(names) if names else None,
            "sw1Name": levels.get("SW1", {}).get("label"),
            "sw2Name": levels.get("SW2", {}).get("label"),
            "sw3Name": levels.get("SW3", {}).get("label"),
        }

    def _ensure_universe_catalog(self) -> dict[str, Any]:
        local_catalog = self._load_universe_catalog()
        if local_catalog and _catalog_is_fresh(local_catalog):
            return local_catalog
        try:
            return self._sync_universe_catalog()
        except Exception:
            if local_catalog:
                return local_catalog
            raise

    def _load_universe_catalog(self) -> dict[str, Any] | None:
        summaries = self.store.rows("universes")
        memberships = self.store.rows("universe_memberships")
        if not summaries or not memberships:
            return None
        return _build_universe_catalog(summaries, memberships)

    def _sync_universe_catalog(self) -> dict[str, Any]:
        summaries = self.tickflow.list_universes()
        if not summaries:
            raise RuntimeError("TickFlow universe list is empty")
        details = self._fetch_universe_details(summaries)
        synced_at = now_text()
        universe_rows = []
        membership_rows = []
        for summary in summaries:
            universe_id = str(summary.get("id") or "").strip()
            if not universe_id:
                continue
            detail = details.get(universe_id) or summary
            symbols = [str(item or "").strip() for item in detail.get("symbols") or [] if str(item or "").strip()]
            universe_rows.append(
                {
                    "id": universe_id,
                    "name": str(detail.get("name") or summary.get("name") or universe_id),
                    "description": detail.get("description") or summary.get("description"),
                    "region": str(detail.get("region") or summary.get("region") or "CN"),
                    "category": str(detail.get("category") or summary.get("category") or ""),
                    "symbolCount": safe_int(detail.get("symbol_count") or detail.get("symbolCount") or summary.get("symbol_count") or summary.get("symbolCount"), len(symbols) or 0) or 0,
                    "syncedAt": synced_at,
                }
            )
            membership_rows.extend({"universeId": universe_id, "symbol": _safe_symbol_label(item)} for item in symbols)
        if not universe_rows or not membership_rows:
            raise RuntimeError("TickFlow universe sync returned no membership rows")
        self.store.replace_where("universes", "id != ''", universe_rows)
        self.store.replace_where("universe_memberships", "universeId != ''", membership_rows)
        return _build_universe_catalog(universe_rows, membership_rows)

    def _fetch_universe_details(self, summaries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        ids = [str(item.get("id") or "").strip() for item in summaries if str(item.get("id") or "").strip()]
        for index in range(0, len(ids), UNIVERSE_BATCH_SIZE):
            chunk = ids[index : index + UNIVERSE_BATCH_SIZE]
            try:
                output.update(self.tickflow.universe_batch(chunk))
                missing = [item for item in chunk if item not in output]
            except Exception:
                missing = chunk
            for universe_id in missing:
                try:
                    detail = self.tickflow.universe(universe_id)
                    if detail:
                        output[universe_id] = detail
                except Exception:
                    continue
        return output

    def _latest_rows(self, table: str, symbol: str, sort_by: str, limit: int) -> list[dict[str, Any]]:
        return sorted([r for r in self.store.rows(table) if r.get("symbol") == symbol], key=lambda r: str(r.get(sort_by) or ""))[-limit:]

    def _analysis_user_prompt(self, symbol: str, watch: dict[str, Any] | None, klines: list[dict[str, Any]], indicators: list[dict[str, Any]], quote: dict[str, Any], financial: Any, news_docs: list[dict[str, Any]]) -> str:
        payload = {"symbol": symbol, "name": (watch or {}).get("name"), "costPrice": (watch or {}).get("costPrice"), "quote": quote, "klines_recent": klines[-30:], "indicators_recent": indicators[-20:], "financial": financial, "news": news_docs}
        return "请分析以下A股材料，注意只使用输入数据。\n" + json.dumps(payload, ensure_ascii=False, indent=2)[:30000]

    def _state_path(self, name: str) -> Path:
        path = Path(self.config.database_path)
        path.mkdir(parents=True, exist_ok=True)
        return path / name

    def _read_state(self, name: str) -> dict[str, Any]:
        path = self._state_path(name)
        with self.state_lock:
            if not path.exists():
                return {}
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return {}

    def _write_state(self, name: str, state: dict[str, Any]) -> None:
        path = self._state_path(name)
        tmp_path = path.with_name(f".{path.name}.{threading.get_ident()}.tmp")
        data = json.dumps(state, ensure_ascii=False, indent=2)
        with self.state_lock:
            tmp_path.write_text(data, encoding="utf-8")
            tmp_path.replace(path)


def _extract_levels(text: str) -> dict[str, Any] | None:
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if not match:
        match = re.search(r"(\{[^{}]*\"current_price\".*?\})", text, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except Exception:
        return None


def _fallback_levels(price: float, cost: Any = None) -> dict[str, Any]:
    return {"current_price": price, "stop_loss": price * 0.92, "breakthrough": price * 1.04, "support": price * 0.96, "cost_level": safe_float(cost), "resistance": price * 1.06, "take_profit": price * 1.12, "gap": None, "target": price * 1.12, "round_number": round(price), "score": 50}


def _level_row(symbol: str, text: str, levels: dict[str, Any]) -> dict[str, Any]:
    return {"symbol": symbol, "analysis_date": today_text(), "current_price": safe_float(levels.get("current_price"), 0) or 0, "stop_loss": safe_float(levels.get("stop_loss")), "breakthrough": safe_float(levels.get("breakthrough")), "support": safe_float(levels.get("support")), "cost_level": safe_float(levels.get("cost_level")), "resistance": safe_float(levels.get("resistance")), "take_profit": safe_float(levels.get("take_profit")), "gap": safe_float(levels.get("gap")), "target": safe_float(levels.get("target")), "round_number": safe_float(levels.get("round_number")), "analysis_text": text, "score": safe_int(levels.get("score"), 50) or 50}


def _levels_from_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {field: row.get(field) for field in ["current_price", "stop_loss", "breakthrough", "support", "cost_level", "resistance", "take_profit", "gap", "target", "round_number", "score"]}


def _build_post_close_review_prompt(item: dict[str, Any], validation: dict[str, Any], composite_text: str, levels: dict[str, Any], market_summary: dict[str, Any] | None, flash_context: dict[str, list[dict[str, Any]]], peer_context: dict[str, Any]) -> str:
    payload = {
        "symbol": item.get("symbol"),
        "name": item.get("name"),
        "costPrice": item.get("costPrice"),
        "sector": _clean_profile_text(item.get("sector")),
        "themes": _normalize_theme_labels(item.get("themes"), company_name=item.get("name")),
        "marketSummary": market_summary,
        "validation": {k: validation.get(k) for k in ["summary", "lines", "verdict", "snapshotDate", "evaluatedTradeDate"]},
        "currentCompositeLevels": levels,
        "compositeAnalysis": _truncate(composite_text, 1800),
        "flashContext": {
            "stockAlerts": [_flash_review_line(row) for row in flash_context.get("stockAlerts", [])[:5]],
            "marketOverview": [_flash_review_line(row) for row in flash_context.get("marketOverviewFlashes", [])[:5]],
        },
        "peerContext": peer_context,
    }
    return "请基于以下 JSON 生成收盘复盘，并按系统要求输出正文和最终 JSON。\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def _normalize_post_close_review(parsed: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    decision = str(parsed.get("decision") or fallback["decision"]).strip()
    if decision not in {"keep", "adjust", "recompute", "invalidate"}:
        decision = fallback["decision"]
    levels = parsed.get("levels") if isinstance(parsed.get("levels"), dict) else None
    if decision != "invalidate" and not levels:
        levels = fallback["levels"]
    normalized = {
        "sessionSummary": str(parsed.get("session_summary") or parsed.get("sessionSummary") or fallback["sessionSummary"]),
        "marketSectorSummary": str(parsed.get("market_sector_summary") or parsed.get("marketSectorSummary") or fallback["marketSectorSummary"]),
        "newsSummary": str(parsed.get("news_summary") or parsed.get("newsSummary") or fallback["newsSummary"]),
        "decision": decision,
        "decisionReason": str(parsed.get("decision_reason") or parsed.get("decisionReason") or fallback["decisionReason"]),
        "actionAdvice": str(parsed.get("action_advice") or parsed.get("actionAdvice") or fallback["actionAdvice"]),
        "marketBias": _normalize_choice(parsed.get("market_bias") or parsed.get("marketBias"), {"tailwind", "neutral", "headwind"}, fallback["marketBias"]),
        "sectorBias": _normalize_choice(parsed.get("sector_bias") or parsed.get("sectorBias"), {"tailwind", "neutral", "headwind"}, fallback["sectorBias"]),
        "newsImpact": _normalize_choice(parsed.get("news_impact") or parsed.get("newsImpact"), {"supportive", "neutral", "disruptive"}, fallback["newsImpact"]),
        "levels": _normalize_review_levels(levels, fallback["levels"]),
    }
    if normalized["decision"] == "invalidate":
        normalized["levels"] = None
    return normalized


def _normalize_choice(value: Any, allowed: set[str], fallback: str) -> str:
    text = str(value or "").strip()
    return text if text in allowed else fallback


def _normalize_review_levels(levels: Any, fallback: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(levels, dict):
        return fallback
    base = dict(fallback or {})
    for field in ["current_price", "stop_loss", "breakthrough", "support", "cost_level", "resistance", "take_profit", "gap", "target", "round_number"]:
        if field in levels:
            base[field] = safe_float(levels.get(field))
    if "score" in levels:
        base["score"] = safe_int(levels.get("score"), safe_int(base.get("score"), 50) or 50)
    return base


def _fallback_post_close_review(validation: dict[str, Any], composite_text: str, levels: dict[str, Any], market_summary: dict[str, Any] | None, flash_context: dict[str, list[dict[str, Any]]], peer_context: dict[str, Any]) -> dict[str, Any]:
    verdict = str(validation.get("verdict") or "unavailable")
    change_pct = safe_float((market_summary or {}).get("dailyChangePct"))
    decision = "recompute" if verdict in {"invalidated", "unavailable"} else ("keep" if verdict == "validated" else "adjust")
    market_bias = "tailwind" if change_pct is not None and change_pct >= 1 else ("headwind" if change_pct is not None and change_pct <= -1 else "neutral")
    stock_alerts = flash_context.get("stockAlerts", [])
    overview = flash_context.get("marketOverviewFlashes", [])
    news_impact = "supportive" if stock_alerts else ("neutral" if overview else "neutral")
    return {
        "sessionSummary": _truncate(_first_nonempty_line(composite_text), 180) or "已完成收盘后综合分析，明日继续按关键位和量价反馈执行。",
        "marketSectorSummary": (peer_context.get("summary") if isinstance(peer_context, dict) else None) or "大盘与板块信息有限，本轮以个股量价结构和关键位验证为主。",
        "newsSummary": _format_flash_review_summary(flash_context),
        "decision": decision,
        "decisionReason": f"昨日关键位{_validation_label(verdict)}，结合今日收盘结构，明日处理建议为{_decision_label(decision)}。",
        "actionAdvice": "明日优先观察开盘量能、是否守住支撑，以及突破位附近的收盘确认；不追高，不把自动分析作为单一交易依据。",
        "marketBias": market_bias,
        "sectorBias": "neutral",
        "newsImpact": news_impact,
        "levels": dict(levels),
    }


def _format_post_close_overview(market_overview: str | None, entries: list[dict[str, Any]]) -> str:
    success = [entry for entry in entries if entry.get("ok")]
    failures = len(entries) - len(success)
    validation_counts = _count_by(str(entry.get("validation", {}).get("verdict") or "unavailable") for entry in success)
    decision_counts = _count_by(str(entry.get("review", {}).get("decision") or "recompute") for entry in success)
    market_counts = _count_by(str(entry.get("review", {}).get("marketBias") or "neutral") for entry in success)
    sector_counts = _count_by(str(entry.get("review", {}).get("sectorBias") or "neutral") for entry in success)
    news_counts = _count_by(str(entry.get("review", {}).get("newsImpact") or "neutral") for entry in success)
    lines = [
        "**🧭 收盘复盘总览**",
        "",
        "**【🌐 市场总览】**",
        market_overview or "未获取到大盘总览，本轮仅输出个股复盘。",
        "",
        "**【📊 本轮统计】**",
        "",
        f"复盘数量: {len(entries)} 只 | 成功 {len(success)} | 失败 {failures}",
        f"关键位验证: 有效 {validation_counts.get('validated', 0)} | 混合 {validation_counts.get('mixed', 0)} | 失效 {validation_counts.get('invalidated', 0)} | 缺样本 {validation_counts.get('unavailable', 0)}",
        f"明日处理: 沿用 {decision_counts.get('keep', 0)} | 微调 {decision_counts.get('adjust', 0)} | 重算 {decision_counts.get('recompute', 0)} | 暂停 {decision_counts.get('invalidate', 0)}",
        f"大盘风向: 顺风 {market_counts.get('tailwind', 0)} | 中性 {market_counts.get('neutral', 0)} | 逆风 {market_counts.get('headwind', 0)}",
        f"板块风向: 顺风 {sector_counts.get('tailwind', 0)} | 中性 {sector_counts.get('neutral', 0)} | 逆风 {sector_counts.get('headwind', 0)}",
        f"新闻影响: 支持 {news_counts.get('supportive', 0)} | 中性 {news_counts.get('neutral', 0)} | 扰动 {news_counts.get('disruptive', 0)}",
    ]
    return "\n".join(lines).strip()


def _format_post_close_detail_message(item: dict[str, Any], validation: dict[str, Any], review: dict[str, Any], market_summary: dict[str, Any] | None, peer_context: dict[str, Any]) -> str:
    levels = review.get("levels") if isinstance(review.get("levels"), dict) else None
    meta = _format_review_market_meta(item, market_summary)
    industry = _format_industry_position(peer_context)
    lines = [
        f"**📘 收盘复盘｜{item.get('name') or item.get('symbol')}（{item.get('symbol')}）**",
        f"{_validation_badge(validation.get('verdict'))} 昨日验证：{_validation_label(validation.get('verdict'))} | {_decision_badge(review.get('decision'))} 明日处理：{_decision_label(review.get('decision'))}",
    ]
    if meta:
        lines.append(meta)
    lines.extend([
        "",
        "**【📍 昨日关键位验证】**",
        f"• 结论：{validation.get('summary') or '暂无验证结论。'}",
        *[f"• {line}" for line in (validation.get("lines") or [])],
        "",
        "**【🧭 今日盘面】**",
        str(review.get("sessionSummary") or "未生成盘面一句话总结。"),
        "",
        "**【🌐 大盘与板块】**",
        " | ".join([part for part in [
            f"• 风向：大盘 {_market_badge(review.get('marketBias'))}{_market_label(review.get('marketBias'))}",
            f"板块 {_market_badge(review.get('sectorBias'))}{_market_label(review.get('sectorBias'))}",
            f"同业 {industry}" if industry else None,
        ] if part]),
        str(review.get("marketSectorSummary") or "未生成大盘/板块总结。"),
        "",
        "**【📰 新闻与公告】**",
        f"• 影响：{_news_badge(review.get('newsImpact'))}{_news_label(review.get('newsImpact'))}",
        str(review.get("newsSummary") or "未生成新闻影响总结。"),
        "",
        "**【🛠️ 明日关键位处理】**",
        f"• 结论：{_decision_badge(review.get('decision'))}{_decision_label(review.get('decision'))}",
        str(review.get("decisionReason") or "未生成处理理由。"),
        "",
        "**【🎯 更新后关键位】**",
    ])
    if str(review.get("decision")) == "invalidate" or not levels:
        lines.extend(["• 已暂停沿用昨日关键位，等待下一轮重算。", "", "**【✅ 操作建议】**", str(review.get("actionAdvice") or "明日先观察，等待新的关键位再执行。")])
        return "\n".join(lines)
    rail = _format_price_rail(levels)
    score = safe_int(levels.get("score"), 50) or 50
    score_suffix = "/10" if score <= 10 else "/100"
    lines.extend([
        f"• 支撑 {fmt_price(levels.get('support'))} | 压力 {fmt_price(levels.get('resistance'))} | 突破 {fmt_price(levels.get('breakthrough'))}",
        f"• 止损 {fmt_price(levels.get('stop_loss'))} | 止盈 {fmt_price(levels.get('take_profit'))} | 评分 {score}{score_suffix}",
        *([f"• 价位框架：{rail}"] if rail else []),
        "",
        "**【✅ 操作建议】**",
        str(review.get("actionAdvice") or "按关键位和次日量价配合再决定是否执行。"),
    ])
    return "\n".join(lines)


def _format_post_close_failure_message(item: dict[str, Any], error_message: str, market_summary: dict[str, Any] | None = None) -> str:
    meta = _format_review_market_meta(item, market_summary)
    lines = [f"**⚠️ 收盘复盘｜{item.get('name') or item.get('symbol')}（{item.get('symbol')}）**"]
    if meta:
        lines.extend(["", meta])
    lines.extend(["", "**【❌ 失败原因】**", error_message, "", "**【🧷 保底处理】**", "本轮未生成可用关键位，请稍后重新执行 `/ta_postclosereview` 或 `/ta_analyze`。"])
    return "\n".join(lines)


def _format_review_market_meta(item: dict[str, Any], market_summary: dict[str, Any] | None) -> str | None:
    parts: list[str] = []
    latest_close = safe_float((market_summary or {}).get("latestClose"))
    change_pct = safe_float((market_summary or {}).get("dailyChangePct"))
    cost = safe_float(item.get("costPrice"))
    if latest_close is not None:
        parts.append(f"• 收盘 {latest_close:.2f}")
    if change_pct is not None:
        parts.append(f"当日 {change_pct:+.2f}%")
    if cost and cost > 0:
        parts.append(f"成本 {cost:.2f}")
    return " | ".join(parts) if parts else None


def _format_price_rail(levels: dict[str, Any]) -> str | None:
    markers = [
        ("⛔止损", levels.get("stop_loss")),
        ("🛡️支撑", levels.get("support")),
        ("💹现价", levels.get("current_price")),
        ("🚧压力", levels.get("resistance")),
        ("🚀突破", levels.get("breakthrough")),
        ("🎯止盈", levels.get("take_profit")),
    ]
    merged: dict[str, list[str]] = {}
    values: dict[str, float] = {}
    for label, value in markers:
        parsed = safe_float(value)
        if parsed is None or parsed <= 0:
            continue
        key = f"{parsed:.2f}"
        values[key] = parsed
        merged.setdefault(key, [])
        if label not in merged[key]:
            merged[key].append(label)
    if len(merged) < 2:
        return None
    return " → ".join(f"{'/'.join(merged[key])} {key}" for key in sorted(merged, key=lambda item: values[item]))


def _build_theme_query(company_name: str, symbol: str) -> str:
    return f"{company_name} {symbol} 所属行业 板块 题材 概念"


def _build_profile_extraction_prompt(symbol: str, company_name: str, documents: list[dict[str, Any]]) -> str:
    blocks = []
    for index, doc in enumerate(documents[:8], 1):
        trunk = str(doc.get("trunk") or "").strip()
        if len(trunk) > 600:
            trunk = trunk[:600] + "..."
        blocks.append(
            "\n".join(
                [
                    f"{index}. 标题: {doc.get('title') or ''}",
                    f"来源: {doc.get('source') or '未知'}",
                    f"时间: {doc.get('publishedAt') or '未知'}",
                    f"正文: {trunk or '无'}",
                ]
            )
        )
    return "\n".join(
        [
            f"股票名称: {company_name}",
            f"股票代码: {symbol}",
            "",
            "请根据下面的妙想搜索结果，提取该股票的行业分类与概念板块，并严格按要求输出 JSON。",
            "",
            "## 妙想搜索结果",
            "\n\n".join(blocks) if blocks else "未获取到任何搜索结果。",
            "",
            "再次提醒：不要输出解释，只输出 JSON。",
        ]
    )


def _extract_json_object(text: str) -> dict[str, Any] | None:
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if not match:
        match = re.search(r"(\{.*\})", text, re.S)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(1))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _clean_profile_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.lower() in {"nan", "none", "null", "n/a", "na", "--"}:
        return None
    if text in {"无", "暂无", "未知", "未识别", "未提及"}:
        return None
    return text


def _normalize_theme_labels(value: Any, company_name: Any = None) -> list[str]:
    raw_items = _theme_raw_items(value)
    labels: list[str] = []
    seen: set[str] = set()
    company = _clean_profile_text(company_name) or ""
    for raw in raw_items:
        for part in re.split(r"[、,，;；|]|\s*/\s*", str(raw or "")):
            label = _clean_theme_label(part)
            if not label or _is_bad_theme_label(label, company) or label in seen:
                continue
            seen.add(label)
            labels.append(label)
            if len(labels) >= 10:
                return labels
    return labels


def _theme_raw_items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        output = []
        for item in value:
            output.extend(_theme_raw_items(item))
        return output
    return [value]


def _clean_theme_label(value: str) -> str | None:
    text = str(value or "").strip()
    text = re.sub(r"[《》\"'“”]", "", text)
    text = re.sub(r"^[：:、，,；;\-]+", "", text)
    text = re.sub(r"[：:、，,；;。]+$", "", text)
    text = re.sub(r"[（(].*?[）)]", "", text)
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"等+$", "", text).strip()
    return _clean_profile_text(text)


def _is_bad_theme_label(label: str, company_name: str = "") -> bool:
    if label in {"公司新闻", "最新公告", "最新新闻", "市场快讯", "公司公告", "行业动态", "板块动态", "题材动态", "概念动态", "资金流向", "龙虎榜"}:
        return True
    if re.search(r"(新闻|公告|快讯|资讯|消息|复盘|头条|Loading)$", label):
        return True
    if re.search(r"(复盘|今日头条|最新消息|Loading|了解一只股票)", label):
        return True
    if company_name and label in {company_name, f"{company_name}股份有限公司", f"{company_name}有限公司"}:
        return True
    if len(label) > 24 and not re.search(r"(概念|板块|行业|设备|能源|电力|金融|消费|医药|材料)$", label):
        return True
    return False


def _join_theme_labels(labels: list[str]) -> str | None:
    cleaned = _normalize_theme_labels(labels)
    return "、".join(cleaned) if cleaned else None


def _profile_changed(original: dict[str, Any], current: dict[str, Any]) -> bool:
    return any(
        (_clean_profile_text(original.get(field)) or "") != (_clean_profile_text(current.get(field)) or "")
        for field in ["sector", "themes", "themeQuery"]
    )


def _format_profile_refresh_line(item: dict[str, Any]) -> str:
    sector = _clean_profile_text(item.get("sector")) or "未识别"
    themes = _join_theme_labels(_normalize_theme_labels(item.get("themes"), company_name=item.get("name"))) or "未识别"
    updated_at = _clean_profile_text(item.get("themeUpdatedAt")) or "未记录"
    return f"• {item.get('name') or item['symbol']}（{item['symbol']}） | 行业: {sector} | 题材: {themes} | 更新时间: {updated_at}"


def _build_universe_catalog(summaries: list[dict[str, Any]], memberships: list[dict[str, Any]]) -> dict[str, Any]:
    summaries_by_id = {str(item.get("id") or "").strip(): item for item in summaries if str(item.get("id") or "").strip()}
    membership_ids_by_symbol: dict[str, list[str]] = {}
    symbols_by_universe_id: dict[str, list[str]] = {}
    for item in memberships:
        universe_id = str(item.get("universeId") or "").strip()
        symbol = _safe_symbol_label(item.get("symbol"))
        if not universe_id or not symbol:
            continue
        for key in {symbol, _safe_normalized_symbol(symbol)}:
            if key:
                _append_unique(membership_ids_by_symbol, key, universe_id)
        _append_unique(symbols_by_universe_id, universe_id, symbol)
    latest_synced_at = max((str(item.get("syncedAt") or "") for item in summaries), default="")
    return {
        "summaries_by_id": summaries_by_id,
        "membership_ids_by_symbol": membership_ids_by_symbol,
        "symbols_by_universe_id": symbols_by_universe_id,
        "synced_at": latest_synced_at,
    }


def _catalog_is_fresh(catalog: dict[str, Any]) -> bool:
    timestamp = _parse_china_timestamp(catalog.get("synced_at"))
    return timestamp is not None and time.time() - timestamp <= UNIVERSE_CACHE_REFRESH_SECONDS


def _parse_china_timestamp(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text):
            parsed = datetime.fromisoformat(text.replace(" ", "T") + "+08:00")
        else:
            parsed = datetime.fromisoformat(text)
        return parsed.timestamp()
    except Exception:
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=8))).timestamp()
        except Exception:
            return None


def _parse_shenwan_universe(summary: dict[str, Any]) -> dict[str, str] | None:
    universe_id = str(summary.get("id") or "")
    match = SHENWAN_UNIVERSE_PATTERN.match(universe_id)
    if not match:
        return None
    level, code = match.groups()
    label = _extract_universe_label(str(summary.get("name") or ""), _clean_profile_text(summary.get("description")))
    if not label:
        return None
    return {"level": level, "code": code, "label": label}


def _extract_universe_label(name: str, description: str | None) -> str | None:
    if description:
        label = re.sub(r"^申万[123]级行业[:：]\s*", "", description).strip()
        if label:
            return label
    return re.sub(r"^SW[123]", "", name).strip() or None


def _safe_symbol_label(value: Any) -> str:
    return str(value or "").strip().upper()


def _safe_normalized_symbol(value: Any) -> str:
    try:
        return normalize_symbol(str(value or ""))
    except Exception:
        return _safe_symbol_label(value)


def _append_unique(mapping: dict[str, list[str]], key: str, value: str) -> None:
    existing = mapping.setdefault(key, [])
    if value not in existing:
        existing.append(value)


def _evaluate_support(snapshot: dict[str, Any], row: dict[str, Any]) -> str:
    support = safe_float(snapshot.get("support"))
    low = safe_float(row.get("low"), 0.0) or 0.0
    close = safe_float(row.get("close"), 0.0) or 0.0
    if not support or support <= 0:
        return "支撑: 昨日未设置支撑位。"
    if low > support * (1 + LEVEL_BUFFER):
        return f"支撑 {support:.2f}: 当日未触达。"
    if close < support * (1 - LEVEL_BUFFER):
        return f"支撑 {support:.2f}: 盘中触达后收盘失守，验证失败。"
    return f"支撑 {support:.2f}: 盘中触达后收盘仍守住，验证有效。"


def _evaluate_resistance(snapshot: dict[str, Any], row: dict[str, Any]) -> str:
    resistance = safe_float(snapshot.get("resistance"))
    high = safe_float(row.get("high"), 0.0) or 0.0
    close = safe_float(row.get("close"), 0.0) or 0.0
    if not resistance or resistance <= 0:
        return "压力: 昨日未设置压力位。"
    if high < resistance * (1 - LEVEL_BUFFER):
        return f"压力 {resistance:.2f}: 当日未触达。"
    if close > resistance * (1 + LEVEL_BUFFER):
        return f"压力 {resistance:.2f}: 当日已被有效站上，原压力失效。"
    return f"压力 {resistance:.2f}: 盘中触达但未有效站上，压制仍在。"


def _evaluate_stop_loss(snapshot: dict[str, Any], row: dict[str, Any]) -> str:
    stop_loss = safe_float(snapshot.get("stop_loss"))
    low = safe_float(row.get("low"), 0.0) or 0.0
    if not stop_loss or stop_loss <= 0:
        return "止损: 昨日未设置止损位。"
    return f"止损 {stop_loss:.2f}: {'已触发' if low <= stop_loss else '未触发'}。"


def _evaluate_take_profit(snapshot: dict[str, Any], row: dict[str, Any]) -> str:
    take_profit = safe_float(snapshot.get("take_profit"))
    high = safe_float(row.get("high"), 0.0) or 0.0
    if not take_profit or take_profit <= 0:
        return "止盈: 昨日未设置止盈位。"
    return f"止盈 {take_profit:.2f}: {'已触发' if high >= take_profit else '未触发'}。"


def _evaluate_breakthrough(snapshot: dict[str, Any], row: dict[str, Any]) -> str:
    breakthrough = safe_float(snapshot.get("breakthrough"))
    high = safe_float(row.get("high"), 0.0) or 0.0
    close = safe_float(row.get("close"), 0.0) or 0.0
    if not breakthrough or breakthrough <= 0:
        return "突破: 昨日未设置突破位。"
    if high < breakthrough:
        return f"突破 {breakthrough:.2f}: 未触发。"
    if close >= breakthrough * (1 + LEVEL_BUFFER):
        return f"突破 {breakthrough:.2f}: 已触发且收盘确认。"
    return f"突破 {breakthrough:.2f}: 盘中试探但收盘未确认。"


def _evaluate_path(snapshot: dict[str, Any], row: dict[str, Any], intraday_rows: list[dict[str, Any]]) -> str:
    stop_loss = safe_float(snapshot.get("stop_loss"))
    take_profit = safe_float(snapshot.get("take_profit"))
    if not stop_loss or not take_profit or stop_loss <= 0 or take_profit <= 0:
        return "路径: 缺少双目标，无法判断先止损还是先止盈。"
    hits_stop = (safe_float(row.get("low"), 0.0) or 0.0) <= stop_loss
    hits_take_profit = (safe_float(row.get("high"), 0.0) or 0.0) >= take_profit
    if not hits_stop and not hits_take_profit:
        return "路径: 当日未触发双目标。"
    if hits_stop and not hits_take_profit:
        return f"路径: 当日先到止损 {stop_loss:.2f}。"
    if not hits_stop and hits_take_profit:
        return f"路径: 当日先到止盈 {take_profit:.2f}。"
    for item in sorted(intraday_rows, key=lambda data: str(data.get("trade_time") or "")):
        intraday_hits_stop = (safe_float(item.get("low"), 0.0) or 0.0) <= stop_loss
        intraday_hits_take_profit = (safe_float(item.get("high"), 0.0) or 0.0) >= take_profit
        if not intraday_hits_stop and not intraday_hits_take_profit:
            continue
        if intraday_hits_stop and not intraday_hits_take_profit:
            return f"路径: 同日双触发中，分钟线判定先到止损 {stop_loss:.2f}。"
        if not intraday_hits_stop and intraday_hits_take_profit:
            return f"路径: 同日双触发中，分钟线判定先到止盈 {take_profit:.2f}。"
        open_price = safe_float(item.get("open"))
        if open_price is not None and open_price <= stop_loss:
            return f"路径: 同日双触发中，分钟线按开盘位置判定先到止损 {stop_loss:.2f}。"
        if open_price is not None and open_price >= take_profit:
            return f"路径: 同日双触发中，分钟线按开盘位置判定先到止盈 {take_profit:.2f}。"
        return "路径: 同日双触发，但分钟线仍无法明确先后。"
    return "路径: 同日双触发，但缺少有效分钟线判定先后。"


def _derive_validation_verdict(support: str, stop_loss: str, take_profit: str, breakthrough: str, path: str) -> str:
    if "验证失败" in support or "已触发" in stop_loss or "先到止损" in path:
        return "invalidated"
    if "已触发" in take_profit or "收盘确认" in breakthrough or "验证有效" in support or "先到止盈" in path:
        return "validated"
    return "mixed"


def _validation_label(value: Any) -> str:
    return {"validated": "验证有效", "invalidated": "明显失效", "mixed": "效果偏混合", "unavailable": "暂无可验证样本"}.get(str(value or "unavailable"), "暂无可验证样本")


def _validation_badge(value: Any) -> str:
    return {"validated": "🟩", "invalidated": "🟥", "mixed": "🟨", "unavailable": "⬜"}.get(str(value or "unavailable"), "⬜")


def _decision_label(value: Any) -> str:
    return {"keep": "沿用", "adjust": "微调", "recompute": "重算", "invalidate": "暂停沿用"}.get(str(value or "recompute"), "重算")


def _decision_badge(value: Any) -> str:
    return {"keep": "🟩", "adjust": "🟨", "recompute": "🟥", "invalidate": "⬛"}.get(str(value or "recompute"), "🟥")


def _market_label(value: Any) -> str:
    return {"tailwind": "顺风", "neutral": "中性", "headwind": "逆风"}.get(str(value or "neutral"), "中性")


def _market_badge(value: Any) -> str:
    return {"tailwind": "🟩", "neutral": "🟨", "headwind": "🟥"}.get(str(value or "neutral"), "🟨")


def _news_label(value: Any) -> str:
    return {"supportive": "支持", "neutral": "中性", "disruptive": "扰动"}.get(str(value or "neutral"), "中性")


def _news_badge(value: Any) -> str:
    return {"supportive": "🟩", "neutral": "🟨", "disruptive": "🟥"}.get(str(value or "neutral"), "🟨")


def _count_by(values: Iterable[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return result


def _flash_review_line(row: dict[str, Any]) -> str:
    published_at = str(row.get("published_at") or "")
    headline = str(row.get("headline") or "").strip()
    content = str(row.get("reason") or row.get("content") or row.get("message") or "").strip()
    prefix = f"[{published_at[11:16]}] " if len(published_at) >= 16 else ""
    body = f"{headline}: {content}" if headline and headline not in content else content
    return prefix + _truncate(body, 220)


def _format_flash_review_summary(flash_context: dict[str, list[dict[str, Any]]]) -> str:
    stock_alerts = [_flash_review_line(row) for row in flash_context.get("stockAlerts", [])[:3]]
    overview = [_flash_review_line(row) for row in flash_context.get("marketOverviewFlashes", [])[:2]]
    if not stock_alerts and not overview:
        return "今日未匹配到显著个股快讯或市场概览快讯，新闻未构成主要解释。"
    parts = []
    if stock_alerts:
        parts.append("个股相关: " + "；".join(stock_alerts))
    if overview:
        parts.append("市场概览: " + "；".join(overview))
    return "\n".join(parts)


def _format_industry_position(context: dict[str, Any] | None) -> str | None:
    if not context:
        return None
    if not context.get("available"):
        return _clean_profile_text(context.get("summary"))
    rank = safe_int(context.get("targetRank"))
    count = safe_int(context.get("peerCount"))
    if not rank or not count:
        return _clean_profile_text(context.get("summary"))
    return f"同业位置 {rank}/{count}"


def _flash_page_items(page: Any) -> list[dict[str, Any]]:
    if not isinstance(page, dict):
        return []
    data = page.get("data")
    if isinstance(data, dict):
        return _flash_page_items(data)
    for key in ["items", "data", "list", "rows"]:
        value = page.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _flash_has_more(page: Any) -> bool:
    if isinstance(page, dict) and isinstance(page.get("data"), dict):
        return _flash_has_more(page["data"])
    return bool(isinstance(page, dict) and (page.get("hasMore") or page.get("has_more") or page.get("hasNext")))


def _flash_next_cursor(page: Any) -> str | None:
    if not isinstance(page, dict):
        return None
    data = page.get("data")
    if isinstance(data, dict):
        return _flash_next_cursor(data)
    value = page.get("nextCursor") or page.get("next_cursor") or page.get("cursor")
    text = str(value or "").strip()
    return text or None


def _format_flash_backfill_status(state: dict[str, Any]) -> str:
    status = "进行中" if state.get("backfillCursor") else "空闲"
    added = safe_int(state.get("lastBackfillStored"), 0) or 0
    return f"{status}（最近补齐 {added} 条）" if added else status


def _pre_market_window() -> dict[str, Any]:
    today = today_text()
    previous = (now_cn() - timedelta(days=1)).strftime("%Y-%m-%d")
    start_at = f"{previous} 17:00:00"
    end_at = f"{today} {PRE_MARKET_BRIEF_READY_TIME}:00"
    return {"startAt": start_at, "endAt": end_at, "startTs": int((_parse_china_timestamp(start_at) or 0) * 1000), "endTs": int((_parse_china_timestamp(end_at) or 0) * 1000)}


def _build_pre_market_prompt(window: dict[str, Any], watchlist: list[dict[str, Any]], flashes: list[dict[str, Any]]) -> str:
    watch_lines = [
        f"- {item.get('name') or item.get('symbol')}（{item.get('symbol')}） 行业: {item.get('sector') or '-'} 题材: {item.get('themes') or '-'}"
        for item in watchlist[:30]
    ]
    flash_lines = [
        f"- [{row.get('published_at')}] {_truncate(str(row.get('content') or ''), 700)}\n  链接: {row.get('url') or '-'}"
        for row in flashes[:12]
    ]
    return "\n".join([
        f"请生成 {str(window['endAt'])[:10]} 的开盘前资讯简报。",
        f"信息窗口: {window['startAt']} ~ {window['endAt']}",
        "",
        "自选股:",
        *watch_lines,
        "",
        "金十数据整理快讯:",
        *flash_lines,
    ])


def _fallback_pre_market_summary(flashes: list[dict[str, Any]], watchlist: list[dict[str, Any]]) -> str:
    matched_symbols = _matched_pre_market_symbols(flashes, watchlist)
    major = "\n".join(f"• [{str(row.get('published_at') or '')[-8:-3]}] {_pre_market_headline(row)}" for row in flashes[:5])
    matched = []
    for item in watchlist:
        cues = []
        normalized_name = _normalize_flash_text(str(item.get("name") or ""))
        for row in flashes:
            text = _normalize_flash_text(str(row.get("content") or ""))
            if normalized_name and normalized_name in text:
                cues.append(_pre_market_headline(row))
            else:
                boards = [keyword for keyword in _flash_board_keywords(item) if _normalize_flash_text(keyword) in text]
                if boards:
                    cues.append(f"{'/'.join(boards[:2])}: {_pre_market_headline(row)}")
            if len(cues) >= 2:
                break
        if cues:
            matched.append(f"• {item.get('name') or item.get('symbol')}（{item.get('symbol')}）: {'；'.join(cues)}")
    return "\n".join([
        "🧭 重大要闻",
        major or "• 暂无可提取的整理快讯。",
        "",
        "🎯 自选相关",
        "\n".join(matched[:5]) if matched else "• 未发现直接命中自选股、行业或题材的盘前整理快讯。",
        "",
        "💡 潜在机会",
        "• 关注快讯中反复出现的政策、AI、算力、机器人、能源、订单和业绩方向是否在竞价阶段获得资金确认。",
        "",
        "⚠️ 风险提示",
        "• 若重大消息只带来高开但缺少量能承接，应优先防范冲高回落；海外宏观、制裁、关税与监管消息需等待后续确认。",
        "",
        "📌 开盘前关注清单",
        f"• 自选直接/题材命中 {len(matched_symbols)} 只，开盘重点看竞价强弱、量能承接与回落后的资金回流。",
    ])


def _matched_pre_market_symbols(flashes: list[dict[str, Any]], watchlist: list[dict[str, Any]]) -> set[str]:
    matched: set[str] = set()
    for row in flashes:
        text = _normalize_flash_text(str(row.get("content") or ""))
        for item in watchlist:
            keywords = _flash_direct_keywords(item) + _flash_board_keywords(item)
            if any(_normalize_flash_text(keyword) in text for keyword in keywords if keyword):
                matched.add(str(item.get("symbol") or ""))
    return matched


def _pre_market_headline(row: dict[str, Any]) -> str:
    text = str(row.get("content") or "").strip()
    text = re.sub(r"^【?金十数据整理】?[:：]?", "", text).strip()
    return _truncate(text, 180)


def _state_heartbeat_stale(state: dict[str, Any], interval_seconds: int, minimum_seconds: int) -> bool:
    if not state.get("running"):
        return False
    heartbeat_at = state.get("lastHeartbeatAt") or state.get("runtimeObservedAt")
    parsed = _parse_china_timestamp(heartbeat_at)
    if parsed is None:
        return False
    threshold = max(int(interval_seconds or 1) * 3, minimum_seconds)
    return time.time() - parsed > threshold


def _format_heartbeat(value: Any, interval_seconds: int, minimum_seconds: int) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = _parse_china_timestamp(text)
    if parsed is None:
        return text
    stale_seconds = max(0, int(time.time() - parsed))
    threshold = max(int(interval_seconds or 1) * 3, minimum_seconds)
    return f"{text}（已超时 {stale_seconds} 秒）" if stale_seconds > threshold else text


def _format_task_result(value: Any) -> str:
    return {"success": "成功", "failed": "失败", "skipped": "跳过", "waiting_daily_update": "等待日更"}.get(str(value or ""), "暂无")


def _format_api_key_level(value: Any) -> str:
    aliases = {"free": "Free", "starter": "Starter", "pro": "Pro", "expert": "Expert"}
    text = str(value or "").strip()
    return aliases.get(text.lower(), text or "-")


def _should_run_scheduled_task(state: dict[str, Any], attempt_key: str, success_key: str, today: str) -> bool:
    return state.get(attempt_key) != today and state.get(success_key) != today


def _should_run_review_task(state: dict[str, Any], today: str) -> bool:
    if state.get("lastReviewSuccessDate") == today:
        return False
    if state.get("lastReviewResultType") == "waiting_daily_update":
        return True
    return state.get("lastReviewAttemptDate") != today


def _monitor_session_key() -> str:
    now = now_cn()
    suffix = "AM" if now.strftime("%H:%M") < "13:00" else "PM"
    return f"{now.strftime('%Y-%m-%d')}_{suffix}"


def _quote_map(quotes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    mapped: dict[str, dict[str, Any]] = {}
    for quote in quotes:
        if not isinstance(quote, dict):
            continue
        symbol = _quote_symbol(quote)
        if symbol:
            mapped[symbol] = quote
    return mapped


def _quote_symbol(quote: dict[str, Any]) -> str | None:
    for field in ["symbol", "ts_code", "code", "secuCode", "证券代码", "代码"]:
        value = quote.get(field)
        if value:
            try:
                return normalize_symbol(str(value))
            except Exception:
                text = str(value or "").strip().upper()
                if text:
                    return text
    return None


def _quote_price(quote: dict[str, Any]) -> float | None:
    ext = quote.get("ext") if isinstance(quote.get("ext"), dict) else {}
    for value in [
        quote.get("last_price"),
        quote.get("lastPrice"),
        quote.get("latest_price"),
        quote.get("latestPrice"),
        quote.get("price"),
        quote.get("close"),
        quote.get("最新价"),
        quote.get("现价"),
        ext.get("last_price"),
        ext.get("lastPrice"),
        ext.get("latestPrice"),
    ]:
        parsed = safe_float(value)
        if parsed is not None:
            return parsed
    return None


def _quote_change_pct(quote: dict[str, Any]) -> float | None:
    ext = quote.get("ext") if isinstance(quote.get("ext"), dict) else {}
    for value in [
        quote.get("change_pct"),
        quote.get("changePct"),
        quote.get("pct_chg"),
        quote.get("percent"),
        quote.get("涨跌幅"),
        quote.get("涨幅"),
        ext.get("change_pct"),
        ext.get("changePct"),
        ext.get("pct_chg"),
    ]:
        parsed = safe_float(value)
        if parsed is not None:
            return parsed
    return None


def _monitor_price_rules(level: dict[str, Any]) -> list[tuple[str, str, str]]:
    rules: list[tuple[str, str, str]] = []
    seen_targets: set[tuple[str, float]] = set()
    for field, op, label in [
        ("stop_loss", "<=", "止损位"),
        ("support", "<=", "支撑位"),
        ("take_profit", ">=", "止盈位"),
        ("breakthrough", ">=", "突破位"),
        ("resistance", ">=", "压力位"),
    ]:
        target = safe_float(level.get(field))
        if target is None or target <= 0:
            continue
        marker = (op, round(target, 4))
        if marker in seen_targets:
            continue
        seen_targets.add(marker)
        rules.append((field, op, label))
    return rules


def _resolve_monitor_session_notification(previous_phase: str | None, current_phase: str, hhmm: str, sent: list[str]) -> dict[str, str] | None:
    if (
        "morning_start" not in sent
        and current_phase == "trading"
        and hhmm <= "11:30"
        and (previous_phase == "pre_market" or _within_hhmm(hhmm, "09:30", "09:40"))
    ):
        return {"id": "morning_start", "title": "🔔 开始上午盯盘", "phaseText": "上午盘开盘"}

    if (
        "morning_end" not in sent
        and current_phase == "lunch_break"
        and (previous_phase == "trading" or _within_hhmm(hhmm, "11:30", "11:40"))
    ):
        return {"id": "morning_end", "title": "🔔 上午盯盘结束", "phaseText": "上午盘收盘"}

    if (
        "afternoon_start" not in sent
        and current_phase == "trading"
        and hhmm >= "13:00"
        and (previous_phase == "lunch_break" or _within_hhmm(hhmm, "13:00", "13:10"))
    ):
        return {"id": "afternoon_start", "title": "🔔 开始下午盯盘", "phaseText": "下午盘开盘"}

    if (
        "day_end" not in sent
        and current_phase == "closed"
        and (previous_phase == "trading" or _within_hhmm(hhmm, "15:00", "15:10"))
    ):
        return {"id": "day_end", "title": "🔔 今日盯盘结束", "phaseText": "今日收盘"}

    return None


def _within_hhmm(value: str, start: str, end: str) -> bool:
    return start <= value <= end


def _format_monitor_system_notification(title: str, lines: list[str]) -> str:
    return f"{title}\n\n" + "\n".join(lines)


def _format_scheduled_notification(kind: str, message: str) -> str:
    if kind != "daily_update":
        return message
    success = str(message or "").lstrip().startswith(("📊", "📋", "✅"))
    lines = _normalize_result_lines(message) if success else _select_update_notification_lines(message)
    title = "📊 定时日更完成" if success else "❌ 定时日更失败"
    return _format_monitor_system_notification(title, lines)


def _select_update_notification_lines(result: str) -> list[str]:
    lines = _normalize_result_lines(result)
    head = lines[:4]
    highlights = [line for line in lines if line.startswith("🏁")]
    return _dedupe_lines([*head, *highlights])[:12]


def _normalize_result_lines(result: str) -> list[str]:
    return [line.strip() for line in str(result or "").splitlines() if line.strip() and not line.strip().startswith("[SILENT]")]


def _dedupe_lines(lines: list[str]) -> list[str]:
    out = []
    seen = set()
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
    return out


def _summarize_task_message(value: str, limit: int = 220) -> str:
    lines = [line.strip() for line in str(value or "").splitlines() if line.strip() and not line.strip().startswith("[SILENT]")]
    return _truncate(" | ".join(lines[:3]), limit) if lines else ""


def _first_nonempty_line(value: str) -> str:
    for line in str(value or "").splitlines():
        text = line.strip()
        if text and not text.startswith("```"):
            return text
    return ""


def _to_flash_record(item: dict[str, Any]) -> dict[str, Any] | None:
    content = str(item.get("content") or item.get("text") or item.get("title") or "").strip()
    raw_time = str(item.get("time") or item.get("published_at") or item.get("publishedAt") or item.get("date") or "").strip()
    url = str(item.get("url") or item.get("link") or "").strip()
    if not content or not raw_time:
        return None
    published_ts = _parse_flash_time_ms(raw_time)
    if published_ts is None:
        return None
    published_at = datetime.fromtimestamp(published_ts / 1000, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    flash_key = url or hash_key(raw_time, content)
    return {"flash_key": flash_key, "published_at": published_at, "published_ts": int(published_ts), "content": content, "url": url, "ingested_at": now_text(), "raw_json": json_text(item.get("raw") or item)}


def _parse_flash_time_ms(value: str) -> int | None:
    text = value.strip()
    if not text:
        return None
    try:
        if text.isdigit():
            number = int(text)
            return number if number > 10_000_000_000 else number * 1000
        normalized = text.replace("Z", "+00:00")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", normalized):
            parsed = datetime.fromisoformat(normalized.replace(" ", "T") + "+08:00")
        else:
            parsed = datetime.fromisoformat(normalized.replace(" ", "T"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
        return int(parsed.timestamp() * 1000)
    except Exception:
        return None


def _sort_flash_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(records, key=lambda item: int(item.get("published_ts") or 0))


def _merge_flash_records(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            key = str(item.get("flash_key") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return _sort_flash_records(merged)


def _filter_alertable_flash_records(records: list[dict[str, Any]], last_poll_at: Any, now_ts: int, poll_interval_seconds: int) -> list[dict[str, Any]]:
    interval_cutoff = now_ts - max(1, int(poll_interval_seconds or 1)) * 1000 - FLASH_ALERT_FRESHNESS_GRACE_SECONDS * 1000
    parsed_last_poll = _parse_china_timestamp(last_poll_at)
    last_poll_ts = int(parsed_last_poll * 1000) if parsed_last_poll is not None else -10**30
    cutoff = max(interval_cutoff, last_poll_ts - FLASH_ALERT_FRESHNESS_GRACE_SECONDS * 1000)
    return [item for item in records if int(item.get("published_ts") or 0) >= cutoff]


def _build_flash_candidates(flashes: list[dict[str, Any]], watchlist: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = []
    for flash in flashes:
        if _should_ignore_flash(str(flash.get("content") or "")):
            continue
        matches = []
        normalized_content = _normalize_flash_text(str(flash.get("content") or ""))
        for item in watchlist:
            direct = [keyword for keyword in _flash_direct_keywords(item) if _normalize_flash_text(keyword) in normalized_content]
            boards = [keyword for keyword in _flash_board_keywords(item) if _normalize_flash_text(keyword) in normalized_content]
            if direct or boards:
                matches.append({"item": item, "directKeywords": direct, "boardKeywords": boards})
        if matches:
            candidates.append({"flash": flash, "matches": matches})
    return candidates


def _flash_direct_keywords(item: dict[str, Any]) -> list[str]:
    symbol = str(item.get("symbol") or "")
    code = ""
    try:
        code = symbol_code(symbol)
    except Exception:
        code = symbol
    return _unique_compact([symbol, code, str(item.get("name") or "")])


def _flash_board_keywords(item: dict[str, Any]) -> list[str]:
    values = []
    sector = _clean_profile_text(item.get("sector"))
    if sector:
        values.extend(re.split(r"[-/|>|→｜]", sector))
    values.extend(_normalize_theme_labels(item.get("themes"), company_name=item.get("name")))
    return [value for value in _unique_compact(values) if _useful_flash_board_keyword(value)]


def _useful_flash_board_keyword(value: str) -> bool:
    text = re.sub(r"\s+", "", value).strip()
    return len(text) >= 2 and not re.search(r"(行业|板块|题材|概念|个股|公司|市场|资讯|公告|快讯|新闻|政策)$", text)


def _should_ignore_flash(content: str) -> bool:
    text = content.strip()
    return any(pattern.search(text) for pattern in FLASH_NOISE_PATTERNS)


def _fallback_flash_decision(candidate: dict[str, Any]) -> dict[str, Any]:
    direct_symbols = [match["item"]["symbol"] for match in candidate["matches"] if match.get("directKeywords")]
    if not direct_symbols:
        return {"alert": False, "importance": "low", "relevantSymbols": [], "headline": "", "reason": ""}
    return {"alert": True, "importance": _infer_flash_importance(str(candidate["flash"].get("content") or "")), "relevantSymbols": _unique_compact(direct_symbols), "headline": "Jin10快讯直接命中自选股", "reason": "快讯直接提及关注股票/公司，建议尽快核实公告、消息来源与盘面反馈。"}


def _resolve_flash_symbols(llm_symbols: list[str], matches: list[dict[str, Any]]) -> list[str]:
    available = {match["item"]["symbol"] for match in matches}
    direct = [match["item"]["symbol"] for match in matches if match.get("directKeywords")]
    normalized = [symbol for symbol in _unique_compact(llm_symbols) if symbol in available]
    return normalized or _unique_compact(direct) or _unique_compact([match["item"]["symbol"] for match in matches])


def _build_flash_alert_message(flash: dict[str, Any], matches: list[dict[str, Any]], decision: dict[str, Any], symbols: list[str]) -> str:
    labels = []
    for symbol in symbols:
        matched = next((match for match in matches if match["item"].get("symbol") == symbol), None)
        labels.append(f"{matched['item'].get('name') or symbol}（{symbol}）" if matched else symbol)
    return "\n".join(
        [
            f"📰 {decision.get('headline') or 'Jin10快讯命中自选'}",
            f"时间: {flash.get('published_at')}",
            f"级别: {_format_flash_importance(str(decision.get('importance') or 'medium'))}",
            f"关联: {'、'.join(labels)}",
            f"判断: {decision.get('reason') or '快讯与当前关注标的相关，建议尽快核实。'}",
            f"快讯: {_truncate(str(flash.get('content') or ''), 260)}",
            f"来源: {flash.get('url') or '-'}",
        ]
    )


def _infer_flash_importance(content: str) -> str:
    return "high" if any(keyword in content for keyword in FLASH_HIGH_IMPORTANCE_KEYWORDS) else "medium"


def _format_flash_importance(value: str) -> str:
    return {"high": "高", "medium": "中", "low": "低"}.get(value, "中")


def _is_night_quiet_hour() -> bool:
    hour = datetime.now(timezone(timedelta(hours=8))).hour
    return hour >= 22 or hour < 6


def _normalize_flash_text(value: str) -> str:
    return value.lower().replace(" ", "").strip()


def _unique_compact(values: list[Any]) -> list[str]:
    output = []
    seen = set()
    for value in values:
        text = re.sub(r"\s+", "", str(value or "")).strip()
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return output


def _truncate(value: str, max_length: int) -> str:
    return value if len(value) <= max_length else value[:max_length] + "..."


def _render_mx_select(keyword: str, result: Any, limit: int) -> str:
    candidates = _extract_candidates(result, limit)
    lines = [f"🧭 智能选股: {keyword}", f"候选数: {len(candidates)}"]
    for idx, item in enumerate(candidates, 1):
        lines.append(f"{idx}. {item.get('name')}（{item.get('symbol')}） 现价 {item.get('latestPrice') or '-'} 涨跌幅 {item.get('changePct') or '-'}")
    if not candidates:
        lines.append(json.dumps(result, ensure_ascii=False)[:3000])
    return "\n".join(lines)


def _extract_candidates(result: Any, limit: int) -> list[dict[str, Any]]:
    rows = _find_rows(result)
    out: list[dict[str, Any]] = []
    for row in rows:
        code = str(row.get("代码") or row.get("股票代码") or row.get("SECURITY_CODE") or row.get("secuCode") or row.get("code") or "").strip()
        name = str(row.get("名称") or row.get("股票简称") or row.get("SECURITY_NAME_ABBR") or row.get("secuName") or row.get("name") or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            match = re.search(r"\b(\d{6})\b", json.dumps(row, ensure_ascii=False))
            code = match.group(1) if match else ""
        if not code:
            continue
        out.append({"symbol": normalize_symbol(code), "name": name or code, "latestPrice": row.get("最新价") or row.get("现价"), "changePct": row.get("涨跌幅") or row.get("涨幅")})
        if len(out) >= limit:
            break
    return out


def _find_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        if all(isinstance(x, dict) for x in value):
            return value
        out = []
        for item in value:
            out.extend(_find_rows(item))
        return out
    if not isinstance(value, dict):
        return []
    for key in ["dataList", "rows", "list", "data"]:
        rows = _find_rows(value.get(key))
        if rows:
            return rows
    return []


def _extract_eastmoney_stocks(value: Any) -> list[dict[str, Any]]:
    rows = _find_rows(value)
    return [{"code": r.get("code") or r.get("secuCode") or r.get("SECURITY_CODE") or r.get("股票代码"), "name": r.get("name") or r.get("secuName") or r.get("SECURITY_NAME_ABBR") or r.get("股票简称")} for r in rows]


def _table_alias(value: str) -> str:
    aliases = {"自选": "watchlist", "日k": "klines_daily", "日线": "klines_daily", "分钟k": "klines_intraday", "指标": "indicators", "关键价位": "key_levels", "分析日志": "analysis_log", "告警日志": "alert_log"}
    return aliases.get(value.lower(), value)


def _send_direct_delivery(target: str, message: str, media_path: Path | None = None) -> tuple[bool, str | None]:
    channel, parts = _parse_delivery_target(target)
    try:
        if channel == "telegram":
            return _send_telegram_direct(parts, message, media_path)
        if channel == "discord":
            return _send_discord_direct(parts, message, media_path)
        return False, f"Hermes send_message 不可用，且当前 alertDeliveryTarget '{target}' 不支持直投递。"
    except Exception as exc:
        return False, str(exc)


def _send_telegram_direct(parts: list[str], message: str, media_path: Path | None = None) -> tuple[bool, str | None]:
    token = _delivery_env("TICKFLOW_ASSIST_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN")
    if not token:
        return False, "Hermes send_message 不可用，且 TELEGRAM_BOT_TOKEN 未配置，无法直投 Telegram。"
    chat_id = parts[0].strip() if parts and parts[0].strip() else _delivery_env("TICKFLOW_ASSIST_TELEGRAM_HOME_CHANNEL", "TELEGRAM_HOME_CHANNEL")
    if not chat_id:
        return False, "Hermes send_message 不可用；alertDeliveryTarget 未包含 Telegram chat_id，且 TELEGRAM_HOME_CHANNEL 未配置。"
    thread_id = parts[1].strip() if len(parts) > 1 and parts[1].strip() else ""
    base_url = f"https://api.telegram.org/bot{token}"
    proxies = _telegram_proxies()
    for chunk in _message_chunks(message, 3900):
        data = {"chat_id": chat_id, "text": chunk}
        if thread_id:
            data["message_thread_id"] = thread_id
        response = _http_post(f"{base_url}/sendMessage", data=data, proxies=proxies)
        error = _delivery_response_error("Telegram", response)
        if error:
            return False, error
    if media_path:
        path = Path(media_path)
        if not path.exists():
            return False, f"Telegram 直投递失败: 附件不存在 {path}"
        method = "sendPhoto" if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"} else "sendDocument"
        field = "photo" if method == "sendPhoto" else "document"
        data = {"chat_id": chat_id}
        if thread_id:
            data["message_thread_id"] = thread_id
        with path.open("rb") as file_obj:
            response = _http_post(f"{base_url}/{method}", data=data, files={field: (path.name, file_obj)}, proxies=proxies)
        error = _delivery_response_error("Telegram", response)
        if error:
            return False, error
    return True, "direct telegram delivery"


def _send_discord_direct(parts: list[str], message: str, media_path: Path | None = None) -> tuple[bool, str | None]:
    token = _delivery_env("TICKFLOW_ASSIST_DISCORD_BOT_TOKEN", "DISCORD_BOT_TOKEN")
    if not token:
        return False, "Hermes send_message 不可用，且 DISCORD_BOT_TOKEN 未配置，无法直投 Discord。"
    channel_id = parts[0].strip() if parts and parts[0].strip() else _delivery_env("TICKFLOW_ASSIST_DISCORD_HOME_CHANNEL", "DISCORD_HOME_CHANNEL")
    if not channel_id:
        return False, "Hermes send_message 不可用；alertDeliveryTarget 未包含 Discord channel_id，且 DISCORD_HOME_CHANNEL 未配置。"
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {token}"}
    for chunk in _message_chunks(message, 1900):
        response = _http_post(url, headers={**headers, "Content-Type": "application/json"}, json={"content": chunk})
        error = _delivery_response_error("Discord", response)
        if error:
            return False, error
    if media_path:
        path = Path(media_path)
        if not path.exists():
            return False, f"Discord 直投递失败: 附件不存在 {path}"
        payload = {"content": ""}
        with path.open("rb") as file_obj:
            response = _http_post(url, headers=headers, data={"payload_json": json.dumps(payload, ensure_ascii=False)}, files={"files[0]": (path.name, file_obj)})
        error = _delivery_response_error("Discord", response)
        if error:
            return False, error
    return True, "direct discord delivery"


def _parse_delivery_target(target: str) -> tuple[str, list[str]]:
    parts = [part.strip() for part in str(target or "").split(":")]
    return (parts[0].lower() if parts else "", parts[1:])


def _message_chunks(message: str, limit: int) -> list[str]:
    text = str(message or "")
    if not text:
        return []
    chunks = []
    remaining = text
    while len(remaining) > limit:
        cut = max(remaining.rfind("\n", 0, limit), remaining.rfind("。", 0, limit), remaining.rfind(" ", 0, limit))
        if cut < max(80, limit // 3):
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _delivery_response_error(platform: str, response: Any) -> str | None:
    status = safe_int(getattr(response, "status_code", 200), 200) or 200
    text = str(getattr(response, "text", "") or "")
    parsed = None
    try:
        parsed = response.json()
    except Exception:
        parsed = None
    if status >= 400:
        return f"{platform} 直投递失败: HTTP {status} {_truncate(text, 300)}".strip()
    if isinstance(parsed, dict) and parsed.get("ok") is False:
        detail = parsed.get("description") or parsed.get("error") or text
        return f"{platform} 直投递失败: {_truncate(str(detail), 300)}"
    if isinstance(parsed, dict) and parsed.get("success") is False:
        return f"{platform} 直投递失败: {_truncate(str(parsed), 300)}"
    return None


def _telegram_proxies() -> dict[str, str] | None:
    proxy = _delivery_env("TICKFLOW_ASSIST_TELEGRAM_PROXY", "TELEGRAM_PROXY")
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _delivery_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    hermes_env = _read_hermes_dotenv()
    for name in names:
        value = hermes_env.get(name)
        if value and value.strip():
            return value.strip()
    return ""


def _read_hermes_dotenv() -> dict[str, str]:
    path = Path.home() / ".hermes" / ".env"
    out: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return out
    for raw in lines:
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        out[key] = value
    return out


def _http_post(url: str, **kwargs: Any) -> Any:
    import requests

    kwargs.setdefault("timeout", 30)
    return requests.post(url, **kwargs)


def _unknown_tool(detail: str | None) -> bool:
    return bool(detail and "Unknown tool: send_message" in detail)
