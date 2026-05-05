from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from .alert_media import AlertCardInput, remove_alert_media, write_alert_card
from .clients import Jin10Client, MxClient, TickFlowClient, call_llm
from .config import Config, load_config, supports_financial, supports_intraday
from .indicators import calculate_indicators
from .storage import LanceStore, SCHEMAS, json_text
from .utils import fmt_price, hash_key, is_trading_time, normalize_symbol, now_text, pct, safe_float, safe_int, symbol_code, today_text


ANALYSIS_SYSTEM = """你是一位A股综合分析师。基于提供的日K、技术指标、实时行情、财务和资讯材料，输出中文分析。
要求：先给100-150字核心摘要，再分“技术面与关键位 / 基本面结论 / 资讯催化与风险 / 共振或冲突与交易判断”展开。
最后必须输出一个 ```json 代码块，字段包含 current_price, stop_loss, breakthrough, support, cost_level, resistance, take_profit, gap, target, round_number, score。
不要编造未提供的数据；不构成投资建议。"""


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
        self.flash_thread: threading.Thread | None = None
        self.flash_stop = threading.Event()

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
            themes = f" | 题材: {item.get('themes')}" if item.get("themes") else ""
            lines.append(f"• {item.get('name') or item['symbol']}（{item['symbol']}） 成本: {fmt_price(item.get('costPrice')) if item.get('costPrice') else '未设置'}{themes}")
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
        for row in target_rows:
            query = f"{row.get('name') or row['symbol']} 所属行业 概念 题材"
            try:
                docs = self.mx.search(query)[:3]
                row["themeQuery"] = query
                row["themes"] = "；".join(doc["title"] for doc in docs if doc.get("title"))[:500]
                row["themeUpdatedAt"] = now_text()
            except Exception as exc:
                row["themes"] = row.get("themes") or f"刷新失败: {exc}"
        self.store.replace_where("watchlist", "symbol != ''", all_rows)
        return f"✅ 已刷新行业/题材信息: {len(target_rows)} 只"

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

    def update_all(self) -> str:
        rows = self.watchlist()
        if not rows:
            return "自选列表为空，无法执行日更。"
        ok, failed = 0, []
        for item in rows:
            try:
                klines = self.fetch_klines(item["symbol"], count=120, persist=True)
                if klines:
                    self.store.replace_where("indicators", f"symbol = '{item['symbol']}'", calculate_indicators(klines))
                if supports_intraday(self.config.tickflow_api_key_level):
                    try:
                        self.fetch_intraday(item["symbol"], count=240)
                    except Exception:
                        pass
                ok += 1
            except Exception as exc:
                failed.append(f"{item['symbol']}: {exc}")
        return "\n".join(["✅ 日更完成", f"成功: {ok}", f"失败: {len(failed)}", *failed[:10]])

    def analyze(self, symbol: str) -> str:
        symbol = normalize_symbol(symbol)
        watch = next((row for row in self.watchlist() if row["symbol"] == symbol), None)
        klines = self._latest_rows("klines_daily", symbol, "trade_date", 120)
        if not klines:
            klines = self.fetch_klines(symbol, 120)
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
        self._write_state("monitor-state.json", {"running": True, "startedAt": now_text(), "lastHeartbeatAt": None, "runtimeHost": "hermes_thread"})
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
        return "\n".join(["📊 监控状态", f"状态: {'✅ 运行中' if state.get('running') else '⭕ 未启动'}", "运行方式: hermes_thread", f"轮询间隔: {self.config.request_interval} 秒", f"最近心跳: {state.get('lastHeartbeatAt') or '暂无'}", self.list_watchlist()])

    def start_daily_update(self) -> str:
        if self.ctx is None:
            return "❌ 需要在 Hermes 会话中启动定时日更，以便调用内置 cronjob 工具。"

        state = self._read_state("daily-update-state.json")
        if state.get("running") and state.get("jobIds"):
            return "\n".join(["✅ TickFlow 定时日更已启动", "运行方式: hermes_cron", f"任务: {', '.join(state.get('jobIds') or [])}"])

        jobs = [
            self._create_cron_job(
                name="TickFlow Assist 日更",
                schedule="25 15 * * 1-5",
                prompt=(
                    "执行 TickFlow Assist A股日更。调用 update_all 工具更新全部自选股行情、K线、指标、财务与分析。"
                    "最终只汇总 update_all 的关键结果；不要调用 send_message，Hermes cron 会自动投递。"
                    "如果自选为空或没有有效更新，最终回复以 [SILENT] 开头。"
                ),
            ),
            self._create_cron_job(
                name="TickFlow Assist 收盘复盘",
                schedule="0 20 * * 1-5",
                prompt=(
                    "执行 TickFlow Assist 收盘复盘。先调用 list_watchlist 获取自选股；"
                    "对每只自选股调用 analyze 工具生成综合分析；最终用中文给出精简复盘摘要。"
                    "不要调用 send_message，Hermes cron 会自动投递。如果自选为空，最终回复以 [SILENT] 开头。"
                ),
            ),
        ]
        job_ids = [job_id for job_id in jobs if job_id]
        if not job_ids:
            state.update({"running": False, "lastErrorAt": now_text(), "lastError": "Hermes cronjob did not return job_id"})
            self._write_state("daily-update-state.json", state)
            return "❌ Hermes cron 任务创建失败，请确认 cronjob 工具在当前 Hermes 会话可用。"
        state.update({"running": True, "startedAt": now_text(), "runtimeHost": "hermes_cron", "jobIds": job_ids})
        self._write_state("daily-update-state.json", state)
        return "\n".join(["✅ TickFlow 定时日更已交给 Hermes cron", "日更: 交易日 15:25", "复盘: 交易日 20:00", f"任务: {', '.join(job_ids)}"])

    def stop_daily_update(self) -> str:
        state = self._read_state("daily-update-state.json")
        job_ids = list(state.get("jobIds") or [])
        removed: list[str] = []
        if self.ctx is not None:
            for job_id in job_ids:
                try:
                    self.ctx.dispatch_tool("cronjob", {"action": "remove", "job_id": job_id})
                    removed.append(str(job_id))
                except Exception:
                    pass
        state.update({"running": False, "lastStoppedAt": now_text()})
        state.pop("jobIds", None)
        self._write_state("daily-update-state.json", state)
        return "🛑 TickFlow 定时日更已停止" + (f"\n已移除 Hermes cron 任务: {', '.join(removed)}" if removed else "")

    def daily_update_status(self) -> str:
        state = self._read_state("daily-update-state.json")
        return "\n".join(["🕒 盘前资讯 / 定时日更 / 收盘复盘状态", f"状态: {'✅ 运行中' if state.get('running') else '⭕ 未启动'}", "运行方式: hermes_cron", "配置来源: Hermes plugin/env/local.config.json", "调度: 日更 15:25 | 复盘 20:00 | 交易日周一至周五", f"Hermes cron 任务: {', '.join(state.get('jobIds') or []) or '暂无'}", f"最近成功: {state.get('lastSuccessAt') or '由 Hermes cron 输出记录'}", f"最近摘要: {state.get('lastResultSummary') or '请通过 Hermes /cron list 查看任务运行记录'}"])

    def flash_monitor_status(self) -> str:
        rows = self.store.rows("jin10_flash")
        latest = max(rows, key=lambda r: int(r.get("published_ts") or 0), default=None)
        return "\n".join(["📰 Jin10 快讯监控状态", f"配置: {'已配置' if self.jin10.configured() else '未配置'}", f"已存快讯: {len(rows)}", f"最近快讯: {(latest or {}).get('published_at') or '暂无'}", f"内容: {((latest or {}).get('content') or '')[:160]}"])

    def test_alert(self) -> str:
        message = f"🧪 TickFlow 测试告警\n时间: {now_text()}\n说明: 这是一条由 Hermes 插件发出的测试消息。"
        media_path = None
        if self.config.alert_image_enabled:
            try:
                media_path = self._write_alert_card(
                    title="TickFlow 测试告警",
                    label="测试",
                    name="平安银行",
                    symbol="000001.SZ",
                    current_price=12.36,
                    trigger_price=12.18,
                    note="用于验证 Hermes send_message 文本与 MEDIA PNG 投递链路。",
                    points=[("09:30", 12.02), ("10:00", 12.08), ("10:30", 12.12), ("11:30", 12.15), ("13:00", 12.19), ("13:30", 12.23), ("14:00", 12.27), ("14:12", 12.36)],
                    levels={"support": 12.08, "resistance": 12.30, "breakthrough": 12.18, "take_profit": 12.68, "stop_loss": 11.86},
                )
            except Exception:
                media_path = None
        ok, detail = self.send_alert(message, media_path=media_path)
        if media_path:
            remove_alert_media(media_path)
        return "✅ 测试告警发送成功（文本 + PNG）" if ok and media_path else ("✅ 测试告警发送成功（文本）" if ok else f"❌ 测试告警发送失败\n原因: {detail}")

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

    def _create_cron_job(self, name: str, schedule: str, prompt: str) -> str | None:
        payload: dict[str, Any] = {"action": "create", "name": name, "schedule": schedule, "prompt": prompt}
        if not self.config.daily_update_notify:
            payload["deliver"] = "local"
        elif self.config.alert_delivery_target:
            payload["deliver"] = self.config.alert_delivery_target
        result = self.ctx.dispatch_tool("cronjob", payload)
        try:
            parsed = json.loads(result) if isinstance(result, str) else result
            return parsed.get("job_id") if isinstance(parsed, dict) else None
        except Exception:
            return None

    def _monitor_loop(self) -> None:
        while not self.monitor_stop.is_set():
            state = self._read_state("monitor-state.json")
            if state.get("running"):
                state["lastHeartbeatAt"] = now_text()
                self._write_state("monitor-state.json", state)
                if is_trading_time():
                    self._monitor_once()
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

    def _write_alert_card(
        self,
        *,
        title: str,
        label: str,
        name: str,
        symbol: str,
        current_price: float,
        trigger_price: float,
        note: str,
        points: list[tuple[str, float]],
        levels: dict[str, float | None],
    ) -> Path:
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

    def _instrument_name(self, symbol: str) -> str:
        try:
            inst = (self.tickflow.instruments([symbol]) or [{}])[0]
            return inst.get("name") or inst.get("display_name") or symbol
        except Exception:
            return symbol

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
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_state(self, name: str, state: dict[str, Any]) -> None:
        self._state_path(name).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


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
