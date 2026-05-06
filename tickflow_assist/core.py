from __future__ import annotations

import json
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
from .utils import fmt_price, hash_key, is_trading_time, normalize_symbol, now_cn, now_text, pct, safe_float, safe_int, symbol_code, today_text


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

PRE_MARKET_BRIEF_KEYWORD = "金十数据整理"
PRE_MARKET_BRIEF_READY_TIME = "09:20"
DAILY_UPDATE_READY_TIME = "15:25"
POST_CLOSE_REVIEW_READY_TIME = "20:00"
DAILY_SCHEDULE_VERSION = 2
DAILY_UPDATE_LOOP_INTERVAL_SECONDS = 60
MONITOR_STALE_GRACE_SECONDS = 90
DAILY_UPDATE_STALE_GRACE_SECONDS = 20 * 60
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

    def fetch_klines(self, symbol: str, count: int = 90, persist: bool = True) -> list[dict[str, Any]]:
        symbol = normalize_symbol(symbol)
        rows = self.tickflow.klines(symbol, count=count)
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
        if not rows:
            message = "自选列表为空，无法执行日更。"
            self._record_daily_update_result("skipped", message, attempted_at, today)
            return ("[SILENT] " if scheduled else "") + message
        ok, failed = 0, []
        for item in rows:
            try:
                klines = self.fetch_klines(item["symbol"], count=120, persist=True)
                if klines:
                    from .indicators import calculate_indicators

                    self.store.replace_where("indicators", f"symbol = '{item['symbol']}'", calculate_indicators(klines))
                if supports_intraday(self.config.tickflow_api_key_level):
                    try:
                        self.fetch_intraday(item["symbol"], count=240)
                    except Exception:
                        pass
                ok += 1
            except Exception as exc:
                failed.append(f"{item['symbol']}: {exc}")
        message = "\n".join(["✅ 日更完成", f"成功: {ok}", f"失败: {len(failed)}", *failed[:10]])
        self._record_daily_update_result("success" if ok > 0 else "failed", message, attempted_at, today)
        return message

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
            self._sync_pre_market_flash_window(window)
            flashes = [
                row for row in self.store.rows("jin10_flash")
                if window["startTs"] <= int(row.get("published_ts") or 0) <= window["endTs"]
                and PRE_MARKET_BRIEF_KEYWORD in str(row.get("content") or "")
            ]
            flashes = sorted(flashes, key=lambda row: int(row.get("published_ts") or 0), reverse=True)
            message = self._build_pre_market_brief_text(window, rows, flashes)
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
        lines = ["🧭 收盘复盘总览", f"复盘数量: {len(rows)} 只"]
        ok, failed = 0, []
        for item in rows:
            try:
                text = self.analyze(item["symbol"])
                ok += 1
                lines.append(f"• {item.get('name') or item['symbol']}（{item['symbol']}）: {_truncate(_first_nonempty_line(text), 160)}")
            except Exception as exc:
                failed.append(f"{item['symbol']}: {exc}")
        lines.extend([f"成功: {ok}", f"失败: {len(failed)}", *failed[:10]])
        message = "\n".join(lines)
        self._record_review_result("success" if ok > 0 else "failed", message, attempted_at, today)
        return message

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
        latest_price = safe_float(quote.get("last_price"), safe_float(klines[-1].get("close") if klines else None, 0.0)) or 0.0
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
        quotes = {q.get("symbol"): q for q in self.tickflow.quotes([c["symbol"] for c in candidates])}
        lines = [f"🧭 智能选股候选池: {keyword}", f"候选数: {len(candidates)}"]
        for idx, c in enumerate(candidates, 1):
            q = quotes.get(c["symbol"], {})
            lines.append(f"{idx}. {c['name']}（{c['symbol']}） 现价: {fmt_price(q.get('last_price') or c.get('latestPrice'))} 涨跌幅: {pct(safe_float(q.get('change_pct') or c.get('changePct')))}")
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

    def monitor_status(self) -> str:
        state = self._read_state("monitor-state.json")
        thread_alive = bool(self.monitor_thread and self.monitor_thread.is_alive())
        stale = _state_heartbeat_stale(state, self.config.request_interval, MONITOR_STALE_GRACE_SECONDS)
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
        ]
        if state.get("lastLoopError"):
            lines.append(f"最近异常: {state.get('lastLoopErrorAt') or '未知时间'} | {state.get('lastLoopError')}")
        lines.append(self.list_watchlist())
        return "\n".join(lines)

    def start_daily_update(self) -> str:
        state = self._read_daily_state()
        old_job_ids = list(state.get("jobIds") or [])
        state.update({"running": True, "scheduleVersion": DAILY_SCHEDULE_VERSION, "startedAt": state.get("startedAt") or now_text(), "lastStoppedAt": None, "runtimeHost": "hermes_thread", "runtimeObservedAt": now_text(), "jobIds": [], "lastError": None, "lastErrorAt": None})
        self._write_daily_state(state)
        if not self.daily_thread or not self.daily_thread.is_alive():
            self.daily_stop.clear()
            self.daily_thread = threading.Thread(target=self._daily_update_loop, daemon=True)
            self.daily_thread.start()
        lines = ["✅ TickFlow 定时任务已启动", "运行方式: hermes_thread", f"盘前资讯: 交易日 {PRE_MARKET_BRIEF_READY_TIME}", f"日更: 交易日 {DAILY_UPDATE_READY_TIME}", f"复盘: 交易日 {POST_CLOSE_REVIEW_READY_TIME}", f"轮询间隔: {DAILY_UPDATE_LOOP_INTERVAL_SECONDS} 秒"]
        if old_job_ids:
            lines.append(f"已忽略旧 Hermes cron 任务记录: {', '.join(str(item) for item in old_job_ids)}")
        return "\n".join(lines)

    def stop_daily_update(self) -> str:
        self.daily_stop.set()
        state = self._read_daily_state()
        state.update({"running": False, "lastStoppedAt": now_text()})
        state.pop("jobIds", None)
        self._write_daily_state(state)
        return "🛑 TickFlow 定时任务已停止"

    def daily_update_status(self) -> str:
        state = self._read_daily_state()
        today = today_text()
        thread_alive = bool(self.daily_thread and self.daily_thread.is_alive())
        stale = _state_heartbeat_stale(state, DAILY_UPDATE_LOOP_INTERVAL_SECONDS, DAILY_UPDATE_STALE_GRACE_SECONDS)
        if state.get("running") and thread_alive and not stale:
            status = "✅ 运行中"
        elif state.get("running"):
            status = "⚠️ 已启用但后台未正常心跳"
        else:
            status = "⭕ 未启动"
        lines = [
            "🕒 盘前资讯 / 定时日更 / 收盘复盘状态",
            f"状态: {status}",
            "运行方式: hermes_thread",
            "配置来源: Hermes plugin/env/local.config.json",
            f"调度: 盘前资讯 {PRE_MARKET_BRIEF_READY_TIME} | 日更 {DAILY_UPDATE_READY_TIME} | 复盘 {POST_CLOSE_REVIEW_READY_TIME} | 交易日周一至周五",
            f"轮询间隔: {DAILY_UPDATE_LOOP_INTERVAL_SECONDS} 秒",
            f"后台线程: {'存活' if thread_alive else '未运行'}",
            f"最近心跳: {_format_heartbeat(state.get('lastHeartbeatAt'), DAILY_UPDATE_LOOP_INTERVAL_SECONDS, DAILY_UPDATE_STALE_GRACE_SECONDS) or '暂无'}",
            "",
            "盘前资讯:",
            f"• 今日已推送: {'是' if state.get('lastPreMarketSuccessDate') == today else '否'}",
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
        ]
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
        if self.ctx is None:
            return False, "Hermes context unavailable"
        try:
            target = self.config.alert_delivery_target
            if not target:
                return False, "请配置 alertDeliveryTarget，例如 telegram、telegram:CHAT_ID、telegram:CHAT_ID:THREAD_ID、discord:CHANNEL_ID。"
            content = message
            if media_path:
                content = f"{message}\nMEDIA:{media_path}"
            payload = {"action": "send", "target": target, "message": content}
            result = self.ctx.dispatch_tool("send_message", payload)
            parsed = json.loads(result) if isinstance(result, str) else result
            if isinstance(parsed, dict) and parsed.get("error"):
                return False, str(parsed.get("error"))
            if isinstance(parsed, dict) and parsed.get("success") is False:
                return False, str(parsed)
            return True, str(result)
        except Exception as exc:
            return False, str(exc)

    def _monitor_loop(self) -> None:
        while not self.monitor_stop.is_set():
            state = self._read_state("monitor-state.json")
            if state.get("running"):
                state["lastHeartbeatAt"] = now_text()
                state["runtimeObservedAt"] = now_text()
                self._write_state("monitor-state.json", state)
                if is_trading_time():
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
        rows = self.watchlist()
        quotes = {q.get("symbol"): q for q in self.tickflow.quotes([r["symbol"] for r in rows])}
        levels = {r.get("symbol"): r for r in self.store.rows("key_levels")}
        for item in rows:
            quote = quotes.get(item["symbol"]) or {}
            level = levels.get(item["symbol"]) or {}
            price = safe_float(quote.get("last_price"))
            if not price:
                continue
            rules = [("stop_loss", "<=", "止损位"), ("breakthrough", ">=", "突破位"), ("take_profit", ">=", "止盈位")]
            for field, op, label in rules:
                target = safe_float(level.get(field))
                if target is None:
                    continue
                hit = price <= target if op == "<=" else price >= target
                key = hash_key(item["symbol"], field, today_text())
                if hit and key not in {r.get("rule_name") for r in self.store.rows("alert_log") if r.get("symbol") == item["symbol"] and r.get("alert_date") == today_text()}:
                    message = f"【{label}】{item.get('name')}（{item['symbol']}）现价 {price:.2f}，触发位 {target:.2f}"
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
                    self.send_alert(message, media_path=media_path)
                    if media_path:
                        remove_alert_media(media_path)
                    self.store.add("alert_log", [{"symbol": item["symbol"], "alert_date": today_text(), "rule_name": key, "message": message, "triggered_at": now_text()}])

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
        if hhmm >= PRE_MARKET_BRIEF_READY_TIME and _should_run_scheduled_task(state, "lastPreMarketAttemptDate", "lastPreMarketSuccessDate", today):
            self._run_daily_scheduled_action(lambda: self.pre_market_brief(scheduled=True))
        state = self._read_daily_state()
        if hhmm >= DAILY_UPDATE_READY_TIME and _should_run_scheduled_task(state, "lastAttemptDate", "lastSuccessDate", today):
            self._run_daily_scheduled_action(lambda: self.update_all(scheduled=True))
        state = self._read_daily_state()
        if hhmm >= POST_CLOSE_REVIEW_READY_TIME and _should_run_scheduled_task(state, "lastReviewAttemptDate", "lastReviewSuccessDate", today):
            if state.get("lastSuccessDate") != today:
                message = f"今日日更尚未在 {DAILY_UPDATE_READY_TIME} 后成功完成，暂不执行收盘复盘"
                self._record_review_result("skipped", message, now_text(), today)
            else:
                self._run_daily_scheduled_action(lambda: self.post_close_review(scheduled=True))

    def _run_daily_scheduled_action(self, fn) -> None:
        message = fn()
        if self.config.daily_update_notify and not str(message).startswith("[SILENT]"):
            self.send_alert(message)

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

    def _build_pre_market_brief_text(self, window: dict[str, Any], watchlist: list[dict[str, Any]], flashes: list[dict[str, Any]]) -> str:
        header = [
            f"🌅 开盘前资讯简报｜{str(window['endAt'])[:10]}",
            f"信息窗口: {window['startAt']} ~ {window['endAt']}",
            f"整理快讯: {len(flashes)} 条 | 自选: {len(watchlist)} 只 | 规则命中: {len(_matched_pre_market_symbols(flashes, watchlist))} 只",
            "",
        ]
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
    return {"success": "成功", "failed": "失败", "skipped": "跳过"}.get(str(value or ""), "暂无")


def _should_run_scheduled_task(state: dict[str, Any], attempt_key: str, success_key: str, today: str) -> bool:
    return state.get(attempt_key) != today and state.get(success_key) != today


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


def _unknown_tool(detail: str | None) -> bool:
    return bool(detail and "Unknown tool: send_message" in detail)
