from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import tickflow_assist.core as core_module
import tickflow_assist.clients as clients_module
from tickflow_assist import schemas, tools
from tickflow_assist.alert_media import _normalize_points, _scale_trading_x
from tickflow_assist.clients import Jin10Client, _extract_jin10_structured_result, _parse_json_rpc, _parse_json_rpc_batch, _repair_mojibake
from tickflow_assist.core import _flash_has_more, _flash_next_cursor, _flash_page_items
from tickflow_assist.config import Config, load_config
from tickflow_assist.core import App
from tickflow_assist.plugin import register
import tickflow_assist.storage as storage_module
from tickflow_assist.storage import LanceStore
from tickflow_assist.storage import SCHEMAS


class DummyCtx:
    def __init__(self):
        self.tools = {}
        self.skills = {}
        self.hooks = {}
        self.commands = {}

    def register_tool(self, name, toolset, schema, handler):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}

    def register_skill(self, name, skill_md):
        self.skills[name] = skill_md

    def register_hook(self, name, handler):
        self.hooks[name] = handler

    def register_command(self, name, handler, description=""):
        self.commands[name] = {"handler": handler, "description": description}


class DispatchCtx:
    def __init__(self):
        self.calls = []

    def dispatch_tool(self, name, args):
        self.calls.append((name, args))
        return json.dumps({"success": True})


class FakeLanceTable:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.deleted = []

    def delete(self, predicate):
        self.deleted.append(predicate)
        if predicate == "symbol = '002261.SZ'":
            self.rows = [row for row in self.rows if row.get("symbol") != "002261.SZ"]

    def add(self, rows):
        self.rows.extend(dict(row) for row in rows)

    def search(self):
        return self

    def limit(self, value):
        return self

    def to_list(self):
        return [dict(row) for row in self.rows]


def test_registers_all_declared_tools():
    ctx = DummyCtx()
    previous = os.environ.get("TICKFLOW_ASSIST_DISABLE_AUTOSTART")
    os.environ["TICKFLOW_ASSIST_DISABLE_AUTOSTART"] = "1"
    try:
        register(ctx)
    finally:
        if previous is None:
            os.environ.pop("TICKFLOW_ASSIST_DISABLE_AUTOSTART", None)
        else:
            os.environ["TICKFLOW_ASSIST_DISABLE_AUTOSTART"] = previous

    assert set(ctx.tools) == set(schemas.TOOL_SCHEMAS)
    assert ctx.tools["add_stock"]["toolset"] == "tickflow-assist"
    assert "pre_llm_call" in ctx.hooks
    underscore_commands = {
        "ta_addstock",
        "ta_rmstock",
        "ta_analyze",
        "ta_backtest",
        "ta_viewanalysis",
        "ta_watchlist",
        "ta_refreshnames",
        "ta_refreshprofiles",
        "ta_monitorstatus",
        "ta_flashstatus",
        "ta_startmonitor",
        "ta_stopmonitor",
        "ta_updateall",
        "ta_premarketbrief",
        "ta_postclosereview",
        "ta_dailyupdatestatus",
        "ta_startdailyupdate",
        "ta_stopdailyupdate",
        "ta_testalert",
        "ta_screenstocks",
        "ta_screenstocks_llm",
        "ta_debug",
    }
    hyphen_commands = {command.replace("_", "-") for command in underscore_commands}
    assert set(ctx.commands) == underscore_commands | hyphen_commands
    assert "ta-addstock" not in ctx.skills
    assert "debug_status" in ctx.tools


def test_command_handlers_return_text_field():
    ctx = DummyCtx()
    previous = os.environ.get("TICKFLOW_ASSIST_DISABLE_AUTOSTART")
    os.environ["TICKFLOW_ASSIST_DISABLE_AUTOSTART"] = "1"
    try:
        register(ctx)
    finally:
        if previous is None:
            os.environ.pop("TICKFLOW_ASSIST_DISABLE_AUTOSTART", None)
        else:
            os.environ["TICKFLOW_ASSIST_DISABLE_AUTOSTART"] = previous
    original = tools.HANDLERS["list_watchlist"]
    tools.HANDLERS["list_watchlist"] = lambda args: json.dumps({"ok": True, "text": "WATCHLIST"})
    try:
        assert ctx.commands["ta_watchlist"]["handler"]("") == "WATCHLIST"
        assert ctx.commands["ta-watchlist"]["handler"]("") == "WATCHLIST"
    finally:
        tools.HANDLERS["list_watchlist"] = original


def test_lancedb_schema_keeps_existing_fields():
    assert [field for field, _, _ in SCHEMAS["watchlist"]] == [
        "symbol",
        "name",
        "costPrice",
        "addedAt",
        "sector",
        "themes",
        "themeQuery",
        "themeUpdatedAt",
    ]
    assert [field for field, _, _ in SCHEMAS["key_levels"]] == [
        "symbol",
        "analysis_date",
        "current_price",
        "stop_loss",
        "breakthrough",
        "support",
        "cost_level",
        "resistance",
        "take_profit",
        "gap",
        "target",
        "round_number",
        "analysis_text",
        "score",
    ]


def test_lancestore_ensure_opens_existing_table_when_create_races():
    class FakeDb:
        def table_names(self):
            return []

        def create_table(self, name, data=None, schema=None):
            raise RuntimeError(f"Table '{name}' already exists")

        def open_table(self, name):
            return {"opened": name}

    original_arrow_schema = storage_module._arrow_schema
    storage_module._arrow_schema = lambda name: object()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store = LanceStore(tmp)
            store._db = FakeDb()
            assert store.ensure("watchlist", [{"symbol": "002202.SZ"}]) == {"opened": "watchlist"}
    finally:
        storage_module._arrow_schema = original_arrow_schema


def test_lancestore_rows_opens_table_when_table_names_are_stale():
    class FakeDb:
        def __init__(self):
            self.table = FakeLanceTable([{"symbol": "002261.SZ", "name": "拓维信息"}])

        def table_names(self):
            return []

        def open_table(self, name):
            return self.table

    with tempfile.TemporaryDirectory() as tmp:
        store = LanceStore(tmp)
        store._db = FakeDb()

        assert store.rows("watchlist")[0]["symbol"] == "002261.SZ"


def test_lancestore_replace_where_keeps_rows_on_first_create():
    class FakeDb:
        def __init__(self):
            self.table = None

        def table_names(self):
            return []

        def create_table(self, name, data=None, schema=None):
            self.table = FakeLanceTable(data)
            return self.table

        def open_table(self, name):
            if self.table is None:
                raise RuntimeError("missing")
            return self.table

    original_arrow_schema = storage_module._arrow_schema
    storage_module._arrow_schema = lambda name: object()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store = LanceStore(tmp)
            fake_db = FakeDb()
            store._db = fake_db
            store.replace_where("watchlist", "symbol = '002261.SZ'", [{"symbol": "002261.SZ", "name": "拓维信息"}])

        assert fake_db.table.rows == [
            {
                "symbol": "002261.SZ",
                "name": "拓维信息",
                "costPrice": 0.0,
                "addedAt": "",
                "sector": None,
                "themes": None,
                "themeQuery": None,
                "themeUpdatedAt": None,
            }
        ]
        assert fake_db.table.deleted == []
    finally:
        storage_module._arrow_schema = original_arrow_schema


def test_alert_delivery_target_uses_hermes_format_only():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "local.config.json").write_text(
            json.dumps({"plugin": {"alertDeliveryTarget": "telegram:-100123:456"}}),
            encoding="utf-8",
        )
        assert load_config(root).alert_delivery_target == "telegram:-100123:456"


def test_review_meta_does_not_render_nan_change_pct():
    meta = core_module._format_review_market_meta(
        {"symbol": "002558.SZ", "costPrice": 33.37},
        {"latestClose": 31.63, "dailyChangePct": float("nan")},
    )

    assert meta == "• 收盘 31.63 | 成本 33.37"
    assert "nan" not in meta.lower()


def test_post_close_market_summary_prefers_previous_daily_close_for_change_pct():
    class MemoryStore:
        def rows(self, name):
            if name != "klines_daily":
                return []
            return [
                {"symbol": "002558.SZ", "trade_date": "2026-05-12", "close": 31.63, "prev_close": 31.2},
                {"symbol": "002558.SZ", "trade_date": "2026-05-13", "close": 32.38, "prev_close": 32.3735},
            ]

    class FakeTickFlow:
        def quotes(self, symbols):
            return [{"symbol": "002558.SZ", "lastPrice": 32.38, "changePct": 0.02}]

    with tempfile.TemporaryDirectory() as tmp:
        app = App(Config(database_path=tmp))
        app.store = MemoryStore()
        app.tickflow = FakeTickFlow()

        summary = app._post_close_market_summary("002558.SZ")

    assert summary["latestClose"] == 32.38
    assert round(summary["dailyChangePct"], 2) == 2.37


def test_send_alert_uses_hermes_target_and_media_tag():
    with tempfile.TemporaryDirectory() as tmp:
        app = App(Config(database_path=tmp, alert_delivery_target="telegram:-100123", alert_image_enabled=False))
        ctx = DispatchCtx()
        app.set_context(ctx)
        ok, _ = app.send_alert("hello", media_path=Path("/tmp/card.png"))

    assert ok is True
    assert ctx.calls == [
        ("send_message", {"action": "send", "target": "telegram:-100123", "message": "hello\nMEDIA:/tmp/card.png"})
    ]


def test_send_alert_falls_back_to_direct_telegram_when_send_message_tool_is_missing():
    class MissingSendMessageCtx:
        def __init__(self):
            self.calls = []

        def dispatch_tool(self, name, args):
            self.calls.append((name, args))
            return json.dumps({"error": "Unknown tool: send_message"})

    class FakeResponse:
        status_code = 200
        text = '{"ok":true}'

        def json(self):
            return {"ok": True}

    calls = []
    previous_http_post = core_module._http_post
    previous_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
    core_module._http_post = lambda url, **kwargs: calls.append((url, kwargs)) or FakeResponse()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            app = App(Config(database_path=tmp, alert_delivery_target="telegram:-100123:456", alert_image_enabled=False))
            ctx = MissingSendMessageCtx()
            app.set_context(ctx)
            ok, detail = app.send_alert("hello")
    finally:
        core_module._http_post = previous_http_post
        if previous_token is None:
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        else:
            os.environ["TELEGRAM_BOT_TOKEN"] = previous_token

    assert ok is True
    assert detail == "direct telegram delivery"
    assert ctx.calls == [("send_message", {"action": "send", "target": "telegram:-100123:456", "message": "hello"})]
    assert calls[0][0] == "https://api.telegram.org/bottest-token/sendMessage"
    assert calls[0][1]["data"] == {"chat_id": "-100123", "text": "hello", "message_thread_id": "456"}


def test_send_alert_falls_back_to_direct_discord_when_send_message_tool_is_missing():
    class MissingSendMessageCtx:
        def dispatch_tool(self, name, args):
            return json.dumps({"success": False, "error": "Unknown tool: send_message"})

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {}

    calls = []
    previous_http_post = core_module._http_post
    previous_token = os.environ.get("DISCORD_BOT_TOKEN")
    os.environ["DISCORD_BOT_TOKEN"] = "discord-token"
    core_module._http_post = lambda url, **kwargs: calls.append((url, kwargs)) or FakeResponse()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            app = App(Config(database_path=tmp, alert_delivery_target="discord:999888777", alert_image_enabled=False))
            app.set_context(MissingSendMessageCtx())
            ok, detail = app.send_alert("hello discord")
    finally:
        core_module._http_post = previous_http_post
        if previous_token is None:
            os.environ.pop("DISCORD_BOT_TOKEN", None)
        else:
            os.environ["DISCORD_BOT_TOKEN"] = previous_token

    assert ok is True
    assert detail == "direct discord delivery"
    assert calls[0][0] == "https://discord.com/api/v10/channels/999888777/messages"
    assert calls[0][1]["headers"]["Authorization"] == "Bot discord-token"
    assert calls[0][1]["json"] == {"content": "hello discord"}


def test_monitor_once_sends_afternoon_start_notification_once():
    class MemoryStore:
        def __init__(self):
            self.tables = {
                "watchlist": [{"symbol": "002558.SZ", "name": "巨人网络", "costPrice": 32.79, "addedAt": "2026-04-17 09:30:00"}],
                "key_levels": [],
                "alert_log": [],
            }

        def rows(self, name):
            return [dict(row) for row in self.tables.get(name, [])]

        def add(self, name, rows):
            self.tables.setdefault(name, []).extend(dict(row) for row in rows)

    class FakeTickFlow:
        def quotes(self, symbols):
            return []

    fixed_now = datetime(2026, 4, 17, 13, 2, 0, tzinfo=timezone(timedelta(hours=8)))
    previous_now_cn = core_module.now_cn
    previous_now_text = core_module.now_text
    previous_today_text = core_module.today_text
    core_module.now_cn = lambda: fixed_now
    core_module.now_text = lambda: "2026-04-17 13:02:00"
    core_module.today_text = lambda: "2026-04-17"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore()
            app = App(Config(database_path=tmp, calendar_file=str(Path(tmp) / "missing-calendar.txt"), alert_delivery_target="telegram", alert_image_enabled=False))
            app.store = store
            app.tickflow = FakeTickFlow()
            app.set_context(DispatchCtx())
            app._write_state(
                "monitor-state.json",
                {
                    "running": True,
                    "lastObservedPhase": "lunch_break",
                    "lastObservedPhaseDate": "2026-04-17",
                    "sessionNotificationsDate": "2026-04-17",
                    "sessionNotificationsSent": [],
                },
            )

            app._monitor_once()
            app._write_state(
                "monitor-state.json",
                {
                    "running": True,
                    "lastObservedPhase": "lunch_break",
                    "lastObservedPhaseDate": "2026-04-17",
                    "sessionNotificationsDate": "2026-04-17",
                    "sessionNotificationsSent": [],
                },
            )
            app._monitor_once()
            state = app._read_state("monitor-state.json")

        send_calls = [args for name, args in app.ctx.calls if name == "send_message"]
        assert len(send_calls) == 1
        assert "🔔 开始下午盯盘" in send_calls[0]["message"]
        assert "阶段: 下午盘开盘" in send_calls[0]["message"]
        assert store.tables["alert_log"] == [
            {
                "symbol": "__system_session__",
                "alert_date": "2026-04-17_PM",
                "rule_name": "afternoon_start",
                "message": "🔔 开始下午盯盘\n\n时间: 2026-04-17 13:02:00\n阶段: 下午盘开盘\n关注列表: 1只",
                "triggered_at": "2026-04-17 13:02:00",
            }
        ]
        assert state["sessionNotificationsSent"] == ["afternoon_start"]
    finally:
        core_module.now_cn = previous_now_cn
        core_module.now_text = previous_now_text
        core_module.today_text = previous_today_text


def test_monitor_session_notification_retries_after_send_failure_within_window():
    class MemoryStore:
        def __init__(self):
            self.tables = {
                "watchlist": [{"symbol": "002558.SZ", "name": "巨人网络", "costPrice": 32.79, "addedAt": "2026-05-06 09:30:00"}],
                "key_levels": [],
                "alert_log": [],
            }

        def rows(self, name):
            return [dict(row) for row in self.tables.get(name, [])]

        def add(self, name, rows):
            self.tables.setdefault(name, []).extend(dict(row) for row in rows)

    class FakeTickFlow:
        def quotes(self, symbols):
            return []

    class FlakyDispatchCtx:
        def __init__(self):
            self.calls = []

        def dispatch_tool(self, name, args):
            self.calls.append((name, args))
            if len(self.calls) == 1:
                return json.dumps({"success": False, "error": "temporary send failed"})
            return json.dumps({"success": True})

    current = [datetime(2026, 5, 6, 15, 3, 0, tzinfo=timezone(timedelta(hours=8)))]
    previous_now_cn = core_module.now_cn
    previous_now_text = core_module.now_text
    previous_today_text = core_module.today_text
    core_module.now_cn = lambda: current[0]
    core_module.now_text = lambda: current[0].strftime("%Y-%m-%d %H:%M:%S")
    core_module.today_text = lambda: current[0].strftime("%Y-%m-%d")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore()
            app = App(Config(database_path=tmp, calendar_file=str(Path(tmp) / "missing-calendar.txt"), alert_delivery_target="telegram", alert_image_enabled=False))
            app.store = store
            app.tickflow = FakeTickFlow()
            app.set_context(FlakyDispatchCtx())
            app._write_state(
                "monitor-state.json",
                {
                    "running": True,
                    "lastObservedPhase": "trading",
                    "lastObservedPhaseDate": "2026-05-06",
                    "sessionNotificationsDate": "2026-05-06",
                    "sessionNotificationsSent": ["afternoon_start"],
                },
            )

            app._monitor_once()
            failed_state = app._read_state("monitor-state.json")
            current[0] = datetime(2026, 5, 6, 15, 4, 0, tzinfo=timezone(timedelta(hours=8)))
            app._monitor_once()
            state = app._read_state("monitor-state.json")

        send_calls = [args for name, args in app.ctx.calls if name == "send_message"]
        assert len(send_calls) == 2
        assert "🔔 今日盯盘结束" in send_calls[0]["message"]
        assert "🔔 今日盯盘结束" in send_calls[1]["message"]
        assert failed_state["lastObservedPhase"] == "closed"
        assert failed_state["lastSessionNotificationError"] == "temporary send failed"
        assert failed_state["sessionNotificationsSent"] == ["afternoon_start"]
        assert state["sessionNotificationsSent"] == ["afternoon_start", "day_end"]
        assert state["lastSessionNotificationError"] is None
        assert store.tables["alert_log"] == [
            {
                "symbol": "__system_session__",
                "alert_date": "2026-05-06_PM",
                "rule_name": "day_end",
                "message": "🔔 今日盯盘结束\n\n时间: 2026-05-06 15:04:00\n阶段: 今日收盘\n关注列表: 1只",
                "triggered_at": "2026-05-06 15:04:00",
            }
        ]
    finally:
        core_module.now_cn = previous_now_cn
        core_module.now_text = previous_now_text
        core_module.today_text = previous_today_text


def test_monitor_once_alerts_support_with_quote_aliases():
    class MemoryStore:
        def __init__(self):
            self.tables = {
                "watchlist": [{"symbol": "002558.SZ", "name": "巨人网络", "costPrice": 32.79, "addedAt": "2026-05-06 09:30:00"}],
                "key_levels": [
                    {
                        "symbol": "002558.SZ",
                        "analysis_date": "2026-05-06",
                        "current_price": 10.8,
                        "stop_loss": 9.5,
                        "breakthrough": 11.5,
                        "support": 10.3,
                        "cost_level": 32.79,
                        "resistance": 11.0,
                        "take_profit": 12.0,
                        "gap": None,
                        "target": 12.0,
                        "round_number": 11.0,
                        "analysis_text": "levels",
                        "score": 60,
                    }
                ],
                "alert_log": [],
            }

        def rows(self, name):
            return [dict(row) for row in self.tables.get(name, [])]

        def add(self, name, rows):
            self.tables.setdefault(name, []).extend(dict(row) for row in rows)

    class FakeTickFlow:
        def quotes(self, symbols):
            return [{"symbol": "002558", "lastPrice": 10.2, "changePct": -2.1}]

    fixed_now = datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    previous_now_cn = core_module.now_cn
    previous_now_text = core_module.now_text
    previous_today_text = core_module.today_text
    core_module.now_cn = lambda: fixed_now
    core_module.now_text = lambda: "2026-05-06 10:00:00"
    core_module.today_text = lambda: "2026-05-06"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore()
            app = App(Config(database_path=tmp, calendar_file=str(Path(tmp) / "missing-calendar.txt"), alert_delivery_target="telegram", alert_image_enabled=False))
            app.store = store
            app.tickflow = FakeTickFlow()
            app.set_context(DispatchCtx())

            app._monitor_once()
            state = app._read_state("monitor-state.json")

        send_calls = [args for name, args in app.ctx.calls if name == "send_message"]
        assert len(send_calls) == 1
        assert "【支撑位】巨人网络（002558.SZ）现价 10.20，涨跌幅 -2.10%，触发位 10.30" in send_calls[0]["message"]
        assert store.tables["alert_log"][0]["symbol"] == "002558.SZ"
        assert state["lastQuoteCount"] == 1
        assert state["lastKeyLevelCount"] == 1
        assert state["lastPriceAlertCount"] == 1
    finally:
        core_module.now_cn = previous_now_cn
        core_module.now_text = previous_now_text
        core_module.today_text = previous_today_text


def test_alert_points_use_intraday_clock_labels_and_append_quote_time():
    class MemoryStore:
        def __init__(self):
            self.tables = {
                "klines_intraday": [
                    {"symbol": "002558.SZ", "period": "1m", "trade_date": "2026-05-12", "trade_time": "14:59:00", "close": 9.9},
                    {"symbol": "002558.SZ", "period": "1m", "trade_date": "2026-05-13", "trade_time": "09:30:00", "close": 10.0},
                    {"symbol": "002558.SZ", "period": "1m", "trade_date": "2026-05-13", "trade_time": "10:30:00", "close": 10.2},
                    {"symbol": "002558.SZ", "period": "1m", "trade_date": "2026-05-13", "trade_time": "14:00:00", "close": 10.5},
                ],
            }

        def rows(self, name):
            return [dict(row) for row in self.tables.get(name, [])]

    previous_today_text = core_module.today_text
    core_module.today_text = lambda: "2026-05-13"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            app = App(Config(database_path=tmp))
            app.store = MemoryStore()

            points = app._alert_points("002558.SZ", 10.6, {"timestamp": "2026-05-13 14:12:30"})
    finally:
        core_module.today_text = previous_today_text

    assert points == [("09:30", 10.0), ("10:30", 10.2), ("14:00", 10.5), ("14:12", 10.6)]
    assert all(time_label.count(":") == 1 for time_label, _ in points)


def test_alert_points_fall_back_to_realtime_axis_point_without_intraday():
    class MemoryStore:
        def __init__(self):
            self.tables = {
                "klines_intraday": [
                    {"symbol": "002558.SZ", "period": "1m", "trade_date": "2026-05-12", "trade_time": "14:59:00", "close": 9.9},
                ],
            }

        def rows(self, name):
            return [dict(row) for row in self.tables.get(name, [])]

    previous_today_text = core_module.today_text
    core_module.today_text = lambda: "2026-05-13"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            app = App(Config(database_path=tmp))
            app.store = MemoryStore()

            timestamp_ms = str(int(datetime(2026, 5, 13, 14, 12, 30, tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000))
            points = app._alert_points("002558.SZ", 10.6, {"timestamp": timestamp_ms})
    finally:
        core_module.today_text = previous_today_text

    assert points == [("09:30", 10.6), ("14:12", 10.6)]
    assert _scale_trading_x(points[-1][0], 44, 650) < _scale_trading_x("15:00", 44, 650)


def test_start_daily_update_uses_hermes_thread_scheduler():
    class MemoryStore:
        def rows(self, name):
            return []

    with tempfile.TemporaryDirectory() as tmp:
        app = App(Config(database_path=tmp, alert_delivery_target="telegram"))
        app.store = MemoryStore()
        result = app.start_daily_update()
        state = app._read_daily_state()
        try:
            assert "运行方式: hermes_thread" in result
            assert "盘前资讯: 交易日 09:20" in result
            assert state["running"] is True
            assert state["runtimeHost"] == "hermes_thread"
            assert state["jobIds"] == []
            assert app.daily_thread and app.daily_thread.is_alive()
        finally:
            app.stop_daily_update()
            if app.daily_thread:
                app.daily_thread.join(timeout=2)


def test_start_daily_update_migrates_old_two_job_schedule():
    class MemoryStore:
        def rows(self, name):
            return []

    with tempfile.TemporaryDirectory() as tmp:
        app = App(Config(database_path=tmp, alert_delivery_target="telegram"))
        app.store = MemoryStore()
        app._write_daily_state({"running": True, "scheduleVersion": 1, "jobIds": ["old-daily", "old-review"]})
        try:
            result = app.start_daily_update()
            state = app._read_daily_state()
        finally:
            app.stop_daily_update()
            if app.daily_thread:
                app.daily_thread.join(timeout=2)

    assert state["scheduleVersion"] == 2
    assert state["jobIds"] == []
    assert "已忽略旧 Hermes cron 任务记录: old-daily, old-review" in result


def test_daily_update_autostarts_by_default_until_user_stops():
    class MemoryStore:
        def rows(self, name):
            return []

    with tempfile.TemporaryDirectory() as tmp:
        app = App(Config(database_path=tmp, alert_delivery_target="telegram"))
        app.store = MemoryStore()

        assert app.should_autostart_daily_update() is True
        app.stop_daily_update()
        state = app._read_daily_state()
        assert state["disabledByUser"] is True
        assert app.should_autostart_daily_update() is False

        try:
            app.start_daily_update()
            state = app._read_daily_state()
            assert state["disabledByUser"] is False
            assert app.should_autostart_daily_update() is True
        finally:
            app.stop_daily_update()
            if app.daily_thread:
                app.daily_thread.join(timeout=2)


def test_update_all_updates_market_indexes_and_reports_openclaw_style_summary():
    class MemoryStore:
        def __init__(self):
            self.tables = {
                "watchlist": [{"symbol": "002558.SZ", "name": "巨人网络", "costPrice": 32.79, "addedAt": "2026-05-08 09:30:00"}],
                "klines_daily": [],
                "indicators": [],
                "klines_intraday": [],
            }
            self.replace_calls = []

        def rows(self, name):
            return [dict(row) for row in self.tables.get(name, [])]

        def replace_where(self, name, predicate, rows):
            self.replace_calls.append((name, predicate, [dict(row) for row in rows]))
            if name == "klines_daily":
                symbol = predicate.split("'")[1]
                self.tables[name] = [row for row in self.tables[name] if row.get("symbol") != symbol]
                self.tables[name].extend(dict(row) for row in rows)
            else:
                self.tables[name] = [dict(row) for row in rows]

    class FakeTickFlow:
        def __init__(self):
            self.kline_calls = []

        def klines(self, symbol, count=90, period="1d", adjust="forward"):
            self.kline_calls.append((symbol, count, period, adjust))
            return [
                {
                    "symbol": symbol,
                    "trade_date": "2026-05-08",
                    "timestamp": 1778227200000,
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.5,
                    "close": 10.5,
                    "volume": 1000.0,
                    "amount": 10500.0,
                    "prev_close": 10.0,
                }
            ]

    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore()
        tickflow = FakeTickFlow()
        app = App(Config(database_path=tmp, tickflow_api_key_level="free"))
        app.store = store
        app.tickflow = tickflow

        text = app.update_all()

    assert "📊 收盘更新: 1 只股票 + 2 个指数, 获取 90 天日K与当日分钟K" in text
    assert "📈 指数更新:" in text
    assert "✅ 上证指数（000001.SH）: 指数日K 1 根" in text
    assert "✅ 深证成指（399001.SZ）: 指数日K 1 根" in text
    assert "📋 个股更新:" in text
    assert "✅ 巨人网络（002558.SZ）: 个股日K 1 根" in text
    assert "🏁 完成: 指数 2 成功, 0 失败 | 个股 1 成功, 0 失败 (共 1 只)" in text
    assert tickflow.kline_calls == [
        ("000001.SH", 90, "1d", "none"),
        ("399001.SZ", 90, "1d", "none"),
        ("002558.SZ", 90, "1d", "forward"),
    ]
    assert any(call[0] == "klines_daily" and "000001.SH" in call[1] for call in store.replace_calls)
    notification = core_module._format_scheduled_notification("daily_update", text)
    assert notification.startswith("📊 定时日更完成\n\n📊 收盘更新:")
    assert "✅ 深证成指（399001.SZ）" in notification
    assert "✅ 巨人网络（002558.SZ）" in notification
    assert "🏁 完成: 指数 2 成功" in notification


def test_scheduled_post_close_review_sends_overview_and_each_stock_separately():
    message = "\n\n".join(
        [
            "**🧭 收盘复盘总览**\n\n**【📊 本轮统计】**\n复盘数量: 2 只 | 成功 2 | 失败 0",
            "**📘 收盘复盘｜巨人网络（002558.SZ）**\n• 收盘 32.38 | 当日 +2.37%",
            "**📘 收盘复盘｜平潭发展（000592.SZ）**\n• 收盘 12.34 | 当日 +5.39%",
        ]
    )
    with tempfile.TemporaryDirectory() as tmp:
        app = App(Config(database_path=tmp, alert_delivery_target="telegram", alert_image_enabled=False))
        app.set_context(DispatchCtx())

        app._run_daily_scheduled_action(lambda: message, "post_close_review")
        state = app._read_daily_state()

    send_calls = [args for name, args in app.ctx.calls if name == "send_message"]
    assert len(send_calls) == 3
    assert send_calls[0]["message"].startswith("**🧭 收盘复盘总览**")
    assert "巨人网络" in send_calls[1]["message"]
    assert "平潭发展" not in send_calls[1]["message"]
    assert "平潭发展" in send_calls[2]["message"]
    assert state["lastNotificationError"] is None


def test_daily_update_once_skips_stale_premarket_and_runs_daily_update():
    current = [datetime(2026, 5, 6, 15, 26, 0, tzinfo=timezone(timedelta(hours=8)))]
    previous_now_cn = core_module.now_cn
    previous_now_text = core_module.now_text
    previous_today_text = core_module.today_text
    core_module.now_cn = lambda: current[0]
    core_module.now_text = lambda: current[0].strftime("%Y-%m-%d %H:%M:%S")
    core_module.today_text = lambda: current[0].strftime("%Y-%m-%d")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            app = App(Config(database_path=tmp, calendar_file=str(Path(tmp) / "missing-calendar.txt"), daily_update_notify=False))
            calls = []

            def fake_pre_market_brief(scheduled=False):
                calls.append("pre_market")
                app._record_pre_market_result("success", "pre market", core_module.now_text(), core_module.today_text())
                return "pre market"

            def fake_update_all(scheduled=False):
                calls.append("daily_update")
                app._record_daily_update_result("success", "daily update", core_module.now_text(), core_module.today_text())
                return "daily update"

            app.pre_market_brief = fake_pre_market_brief
            app.update_all = fake_update_all

            app._daily_update_once()
            state = app._read_daily_state()

        assert calls == ["daily_update"]
        assert state["lastPreMarketAttemptDate"] == "2026-05-06"
        assert state["lastPreMarketResultType"] == "skipped"
        assert "不再补跑盘前资讯" in state["lastPreMarketResultSummary"]
        assert state["lastAttemptDate"] == "2026-05-06"
        assert state["lastSuccessDate"] == "2026-05-06"
    finally:
        core_module.now_cn = previous_now_cn
        core_module.now_text = previous_now_text
        core_module.today_text = previous_today_text


def test_pre_market_brief_uses_cached_flashes_when_sync_times_out():
    class MemoryStore:
        def __init__(self):
            published_at = "2026-05-12 08:30:00"
            self.tables = {
                "watchlist": [{"symbol": "002558.SZ", "name": "巨人网络", "costPrice": 32.79, "addedAt": "2026-05-06 09:30:00", "sector": "传媒", "themes": "游戏"}],
                "jin10_flash": [
                    {
                        "flash_key": "cached-1",
                        "published_at": published_at,
                        "published_ts": int((core_module._parse_china_timestamp(published_at) or 0) * 1000),
                        "content": "金十数据整理：A股每日市场要闻回顾 测试政策消息，游戏板块关注。",
                        "url": "",
                        "ingested_at": published_at,
                        "raw_json": "{}",
                    }
                ],
            }

        def rows(self, name):
            return [dict(row) for row in self.tables.get(name, [])]

    class TimeoutJin10:
        def configured(self):
            return True

        def list_flash(self, cursor=None):
            raise TimeoutError("Read timed out")

    fixed_now = datetime(2026, 5, 12, 9, 21, 0, tzinfo=timezone(timedelta(hours=8)))
    previous_now_cn = core_module.now_cn
    previous_now_text = core_module.now_text
    previous_today_text = core_module.today_text
    core_module.now_cn = lambda: fixed_now
    core_module.now_text = lambda: "2026-05-12 09:21:00"
    core_module.today_text = lambda: "2026-05-12"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            app = App(Config(database_path=tmp, jin10_api_token="token", llm_api_key=""))
            app.store = MemoryStore()
            app.jin10 = TimeoutJin10()

            text = app.pre_market_brief(scheduled=True)
            state = app._read_daily_state()

    finally:
        core_module.now_cn = previous_now_cn
        core_module.now_text = previous_now_text
        core_module.today_text = previous_today_text

    assert "⚠️ 本轮金十同步异常，已使用本地缓存生成简报: Read timed out" in text
    assert "🧭 重大要闻" in text
    assert "失败" not in text.splitlines()[0]
    assert state["lastPreMarketResultType"] == "success"
    assert state["lastPreMarketSuccessDate"] == "2026-05-12"


def test_daily_update_review_retries_after_waiting_for_daily_update():
    current = [datetime(2026, 5, 6, 20, 1, 0, tzinfo=timezone(timedelta(hours=8)))]
    previous_now_cn = core_module.now_cn
    previous_now_text = core_module.now_text
    previous_today_text = core_module.today_text
    core_module.now_cn = lambda: current[0]
    core_module.now_text = lambda: current[0].strftime("%Y-%m-%d %H:%M:%S")
    core_module.today_text = lambda: current[0].strftime("%Y-%m-%d")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            app = App(Config(database_path=tmp, calendar_file=str(Path(tmp) / "missing-calendar.txt"), daily_update_notify=False))
            review_calls = []

            def fake_post_close_review(scheduled=False):
                review_calls.append("review")
                app._record_review_result("success", "review", core_module.now_text(), core_module.today_text())
                return "review"

            app.post_close_review = fake_post_close_review
            app._write_daily_state({
                "running": True,
                "lastPreMarketAttemptDate": "2026-05-06",
                "lastAttemptDate": "2026-05-06",
                "lastResultType": "failed",
            })

            app._daily_update_once()
            waiting_state = app._read_daily_state()
            current[0] = datetime(2026, 5, 6, 20, 2, 0, tzinfo=timezone(timedelta(hours=8)))
            waiting_state.update({"lastSuccessAt": "2026-05-06 21:54:00", "lastSuccessDate": "2026-05-06"})
            app._write_daily_state(waiting_state)
            app._daily_update_once()
            state = app._read_daily_state()

        assert review_calls == ["review"]
        assert waiting_state["lastReviewResultType"] == "waiting_daily_update"
        assert state["lastReviewResultType"] == "success"
        assert state["lastReviewSuccessDate"] == "2026-05-06"
    finally:
        core_module.now_cn = previous_now_cn
        core_module.now_text = previous_now_text
        core_module.today_text = previous_today_text


def test_post_close_review_formats_detail_and_persists_review_snapshot():
    class MemoryTable:
        def __init__(self, store, name):
            self.store = store
            self.name = name

        def delete(self, predicate):
            if "symbol = '002558.SZ'" in predicate:
                self.store.tables[self.name] = [row for row in self.store.tables.get(self.name, []) if row.get("symbol") != "002558.SZ"]

    class MemoryStore:
        def __init__(self):
            self.tables = {
                "watchlist": [{"symbol": "002558.SZ", "name": "巨人网络", "costPrice": 32.79, "addedAt": "2026-05-06 09:30:00", "sector": "传媒", "themes": "游戏"}],
                "klines_daily": [
                    {"symbol": "002558.SZ", "trade_date": "2026-05-05", "timestamp": 1, "open": 10.0, "high": 10.8, "low": 9.9, "close": 10.5, "volume": 1, "amount": 1, "prev_close": 10.1},
                    {"symbol": "002558.SZ", "trade_date": "2026-05-06", "timestamp": 2, "open": 10.6, "high": 11.1, "low": 10.2, "close": 10.8, "volume": 1, "amount": 1, "prev_close": 10.5},
                ],
                "klines_intraday": [],
                "key_levels": [],
                "key_levels_history": [
                    {"symbol": "002558.SZ", "analysis_date": "2026-05-05", "activated_at": "2026-05-05 20:00:00", "profile": "composite", "current_price": 10.5, "stop_loss": 9.5, "breakthrough": 11.0, "support": 10.0, "cost_level": 32.79, "resistance": 10.9, "take_profit": 11.8, "gap": None, "target": 11.8, "round_number": 10.0, "analysis_text": "昨日关键位", "score": 60},
                ],
                "jin10_flash_delivery": [
                    {"flash_key": "flash-1", "published_at": "2026-05-06 18:00:00", "symbols_json": "[\"002558.SZ\"]", "headline": "巨人网络公告", "reason": "巨人网络发布新品进展。", "importance": "medium", "message": "msg", "delivered_at": "2026-05-06 18:01:00"},
                ],
                "jin10_flash": [
                    {"flash_key": "flash-2", "published_at": "2026-05-06 17:00:00", "published_ts": 1, "content": "金十数据整理：A股每日市场要闻回顾 测试内容", "url": "", "ingested_at": "2026-05-06 17:01:00", "raw_json": "{}"},
                ],
            }

        def rows(self, name):
            return [dict(row) for row in self.tables.get(name, [])]

        def add(self, name, rows):
            self.tables.setdefault(name, []).extend(dict(row) for row in rows)

        def replace_where(self, name, predicate, rows):
            symbol = "002558.SZ" if "002558.SZ" in predicate else None
            existing = self.tables.setdefault(name, [])
            if symbol:
                existing = [row for row in existing if row.get("symbol") != symbol]
            self.tables[name] = existing + [dict(row) for row in rows]

        def open(self, name):
            return MemoryTable(self, name)

    class FakeTickFlow:
        def quotes(self, symbols):
            return []

    fixed_now = datetime(2026, 5, 6, 20, 5, 0, tzinfo=timezone(timedelta(hours=8)))
    previous_now_cn = core_module.now_cn
    previous_now_text = core_module.now_text
    previous_today_text = core_module.today_text
    core_module.now_cn = lambda: fixed_now
    core_module.now_text = lambda: "2026-05-06 20:05:00"
    core_module.today_text = lambda: "2026-05-06"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore()
            app = App(Config(database_path=tmp, llm_api_key="", daily_update_notify=False))
            app.store = store
            app.tickflow = FakeTickFlow()

            def fake_analyze(symbol):
                levels = {"current_price": 10.8, "stop_loss": 9.8, "breakthrough": 11.2, "support": 10.3, "cost_level": 32.79, "resistance": 11.0, "take_profit": 12.0, "gap": None, "target": 12.0, "round_number": 11.0, "score": 65}
                store.replace_where("key_levels", f"symbol = '{symbol}'", [{**levels, "symbol": symbol, "analysis_date": "2026-05-06", "analysis_text": "### 核心摘要\n测试分析"}])
                return "### 核心摘要\n测试分析"

            app.analyze = fake_analyze
            text = app.post_close_review()

        assert "**🧭 收盘复盘总览**" in text
        assert "**📘 收盘复盘｜巨人网络（002558.SZ）**" in text
        assert "**【📍 昨日关键位验证】**" in text
        assert "突破 11.00" in text
        assert "**【🛠️ 明日关键位处理】**" in text
        assert "价位框架" in text
        assert "巨人网络公告" in text
        assert store.tables["key_levels"][0]["analysis_text"].startswith("**📘 收盘复盘")
        assert any(row.get("analysis_date") == "2026-05-06" and row.get("profile") == "composite" for row in store.tables["key_levels_history"])
    finally:
        core_module.now_cn = previous_now_cn
        core_module.now_text = previous_now_text
        core_module.today_text = previous_today_text


def test_post_close_market_overview_falls_back_to_index_klines():
    class MemoryStore:
        def __init__(self):
            self.tables = {
                "jin10_flash": [],
                "klines_daily": [
                    {"symbol": "000001.SH", "trade_date": "2026-05-12", "close": 4214.49, "prev_close": 4225.02},
                    {"symbol": "399001.SZ", "trade_date": "2026-05-12", "close": 15824.92, "prev_close": 15899.30},
                ],
            }

        def rows(self, name):
            return [dict(row) for row in self.tables.get(name, [])]

    class FakeTickFlow:
        def quotes(self, symbols):
            return []

    with tempfile.TemporaryDirectory() as tmp:
        app = App(Config(database_path=tmp))
        app.store = MemoryStore()
        app.tickflow = FakeTickFlow()

        overview = app._post_close_market_overview("2026-05-12")

    assert "上证指数（000001.SH），收 4214.49，当日 -0.25%" in overview
    assert "深证成指（399001.SZ），收 15824.92，当日 -0.47%" in overview


def test_daily_update_status_recovers_stale_running_state():
    with tempfile.TemporaryDirectory() as tmp:
        app = App(Config(database_path=tmp))
        app._write_daily_state(
            {
                "running": True,
                "lastHeartbeatAt": "2026-05-05 15:21:08",
                "runtimeHost": "hermes_thread",
            }
        )

        text = app.daily_update_status()
        state = app._read_daily_state()

        app.stop_daily_update()
        if app.daily_thread:
            app.daily_thread.join(timeout=2)

    assert "状态: ✅ 运行中" in text
    assert "运行方式: hermes_thread" in text
    assert "后台线程: 存活" in text
    assert "自动恢复: 已重新启动定时日更线程" in text
    assert state["running"] is True


def test_daily_update_status_reports_premarket_generation_separately_from_delivery():
    previous_today_text = core_module.today_text
    core_module.today_text = lambda: "2026-05-07"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            app = App(Config(database_path=tmp))
            app._write_daily_state(
                {
                    "running": False,
                    "disabledByUser": True,
                    "lastPreMarketSuccessDate": "2026-05-07",
                    "lastPreMarketSuccessAt": "2026-05-07 09:20:53",
                    "lastNotificationError": "Unknown tool: send_message",
                    "lastNotificationErrorAt": "2026-05-07 09:21:21",
                }
            )

            text = app.daily_update_status()
    finally:
        core_module.today_text = previous_today_text

    assert "• 今日已生成: 是" in text
    assert "今日已推送" not in text
    assert "最近投递异常: 2026-05-07 09:21:21 | Unknown tool: send_message" in text


def test_monitor_status_marks_stale_running_state():
    class MemoryStore:
        def rows(self, name):
            return []

    with tempfile.TemporaryDirectory() as tmp:
        app = App(Config(database_path=tmp, request_interval=30))
        app.store = MemoryStore()
        app._write_state(
            "monitor-state.json",
            {"running": True, "lastHeartbeatAt": "2026-05-05 15:21:08", "runtimeHost": "hermes_thread"},
        )

        text = app.monitor_status()

    assert "状态: ⚠️ 已启用但后台未正常心跳" in text
    assert "后台线程: 未运行" in text
    assert "已超时" in text


def test_flash_monitor_status_renders_runtime_state_and_latest_flash():
    class MemoryStore:
        def __init__(self):
            self.tables = {
                "watchlist": [{"symbol": "002261.SZ", "name": "拓维信息", "addedAt": "2026-05-05 09:30:00"}],
                "jin10_flash": [
                    {
                        "flash_key": "https://flash.jin10.com/detail/1",
                        "published_at": "2026-05-05 21:50:06",
                        "published_ts": 1777998606000,
                        "content": "金十快讯测试正文",
                        "url": "https://flash.jin10.com/detail/1",
                    }
                ],
                "jin10_flash_delivery": [{"flash_key": "1", "delivered_at": "2026-05-05 21:51:00"}],
            }

        def rows(self, name):
            return [dict(row) for row in self.tables.get(name, [])]

    class AliveThread:
        def is_alive(self):
            return True

    with tempfile.TemporaryDirectory() as tmp:
        app = App(Config(database_path=tmp, jin10_api_token="token"))
        app.store = MemoryStore()
        app.flash_thread = AliveThread()
        app._write_flash_state(
            {
                "lastHeartbeatAt": "2026-05-05 22:14:43",
                "lastPollAt": "2026-05-05 22:04:30",
                "lastPollStored": 3,
                "lastPollCandidates": 1,
                "lastPollAlerts": 0,
                "backfillCursor": "cursor",
                "lastPrunedAt": "2026-05-05 21:39:17",
                "lastLoopError": "fetch failed",
                "lastLoopErrorAt": "2026-05-05 22:14:53",
            }
        )
        text = app.flash_monitor_status()

    assert "状态: 后台轮询中" in text
    assert "轮询间隔: 300 秒" in text
    assert "关注列表: 1只" in text
    assert "最近一轮: 入库 3 条 | 候选 1 条 | 告警 0 条" in text
    assert "续页补齐: 进行中" in text
    assert "最近异常: 2026-05-05 22:14:53 | fetch failed" in text
    assert "最新快讯:" in text


def test_flash_monitor_counts_backfill_separately_from_latest_poll():
    class MemoryStore:
        def __init__(self):
            self.tables = {
                "watchlist": [],
                "jin10_flash": [
                    {
                        "flash_key": "https://flash.jin10.com/detail/anchor",
                        "published_at": "2026-05-05 23:00:00",
                        "published_ts": 1778002800000,
                        "content": "anchor",
                        "url": "https://flash.jin10.com/detail/anchor",
                    }
                ],
                "jin10_flash_delivery": [],
            }

        def rows(self, name):
            return [dict(row) for row in self.tables.get(name, [])]

        def add(self, name, rows):
            self.tables.setdefault(name, []).extend(dict(row) for row in rows)

    class FakeJin10:
        def configured(self):
            return True

        def list_flash(self, cursor=None):
            if cursor == "cursor-backfill":
                return {
                    "data": {
                        "items": [
                            {"content": "历史补齐1", "time": "2026-05-05T22:50:00+08:00", "url": "https://flash.jin10.com/detail/old1"},
                            {"content": "历史补齐2", "time": "2026-05-05T22:45:00+08:00", "url": "https://flash.jin10.com/detail/old2"},
                        ],
                        "next_cursor": "cursor-next",
                        "has_more": True,
                    }
                }
            return {
                "data": {
                    "items": [
                        {"content": "最新快讯", "time": "2026-05-05T23:05:00+08:00", "url": "https://flash.jin10.com/detail/new1"},
                        {"content": "anchor", "time": "2026-05-05T23:00:00+08:00", "url": "https://flash.jin10.com/detail/anchor"},
                    ],
                    "next_cursor": "cursor-latest",
                    "has_more": True,
                }
            }

    with tempfile.TemporaryDirectory() as tmp:
        app = App(Config(database_path=tmp, jin10_api_token="token"))
        app.store = MemoryStore()
        app.jin10 = FakeJin10()
        app._write_flash_state(
            {
                "initialized": True,
                "lastSeenKey": "https://flash.jin10.com/detail/anchor",
                "lastSeenPublishedAt": "2026-05-05 23:00:00",
                "lastSeenUrl": "https://flash.jin10.com/detail/anchor",
                "backfillCursor": "cursor-backfill",
                "lastPollAt": "2026-05-05 23:00:00",
                "lastPrunedAt": "2999-01-01 00:00:00",
            }
        )

        app._flash_monitor_once()
        state = app._read_flash_state()
        status = app.flash_monitor_status()

    assert state["lastPollStored"] == 1
    assert state["lastBackfillStored"] == 2
    assert state["backfillCursor"] == "cursor-next"
    assert "最近一轮: 入库 1 条" in status
    assert "续页补齐: 进行中（最近补齐 2 条）" in status


def test_jin10_client_reinitializes_when_mcp_session_expires():
    class FakeResponse:
        def __init__(self, status_code, text, headers=None):
            self.status_code = status_code
            self.text = text
            self.content = text.encode("utf-8")
            self.headers = headers or {}
            self.ok = status_code < 400
            self.encoding = "utf-8"
            self.apparent_encoding = "utf-8"

    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": dict(headers or {}), "json": json, "timeout": timeout})
        if len(calls) == 1:
            return FakeResponse(404, "session not found")
        if json and json.get("method") == "initialize":
            return FakeResponse(
                200,
                json_module.dumps({"jsonrpc": "2.0", "id": json["id"], "result": {"protocolVersion": "2025-11-25"}}),
                {"mcp-session-id": "new-session"},
            )
        if json and json.get("method") == "notifications/initialized":
            return FakeResponse(202, "")
        if json and json.get("method") in {"tools/list", "resources/list"}:
            return FakeResponse(200, json_module.dumps({"jsonrpc": "2.0", "id": json["id"], "result": {}}))
        return FakeResponse(
            200,
            json_module.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": json["id"],
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json_module.dumps({"data": {"items": [], "has_more": False}}, ensure_ascii=False),
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            ),
        )

    json_module = json
    previous_post = clients_module.requests.post
    clients_module.requests.post = fake_post
    try:
        client = Jin10Client(Config(jin10_mcp_url="https://mcp.jin10.com/mcp", jin10_api_token="token"))
        client.initialized = True
        client.session_id = "expired-session"
        result = client.list_flash()
    finally:
        clients_module.requests.post = previous_post

    assert result == {"data": {"items": [], "has_more": False}}
    assert calls[0]["headers"]["mcp-session-id"] == "expired-session"
    assert "mcp-session-id" not in calls[1]["headers"]
    assert calls[2]["headers"]["mcp-session-id"] == "new-session"
    assert calls[-1]["headers"]["mcp-session-id"] == "new-session"
    assert client.session_id == "new-session"
    assert client.initialized is True


def test_refresh_profiles_uses_tickflow_universes_and_drops_news_titles():
    class MemoryStore:
        def __init__(self, rows):
            self.tables = {"watchlist": [dict(row) for row in rows]}

        def rows(self, name):
            return [dict(row) for row in self.tables.get(name, [])]

        def replace_where(self, name, predicate, rows):
            self.tables[name] = [dict(row) for row in rows]

    class FakeTickFlow:
        def list_universes(self):
            return [
                {"id": "CN_Equity_SW1_801730", "name": "SW1电力设备", "description": "申万1级行业: 电力设备", "region": "CN", "category": "industry", "symbol_count": 1},
                {"id": "CN_Equity_SW2_801735", "name": "SW2风电设备", "description": "申万2级行业: 风电设备", "region": "CN", "category": "industry", "symbol_count": 1},
                {"id": "CN_Equity_SW3_857331", "name": "SW3风电整机", "description": "申万3级行业: 风电整机", "region": "CN", "category": "industry", "symbol_count": 1},
            ]

        def universe_batch(self, ids):
            summaries = {item["id"]: item for item in self.list_universes()}
            return {universe_id: {**summaries[universe_id], "symbols": ["002202.SZ"]} for universe_id in ids}

        def universe(self, universe_id):
            return self.universe_batch([universe_id]).get(universe_id)

    with tempfile.TemporaryDirectory() as tmp:
        app = App(Config(database_path=tmp))
        app.tickflow = FakeTickFlow()
        app.store = MemoryStore(
            [
                {
                    "symbol": "002202.SZ",
                    "name": "金风科技",
                    "costPrice": 0,
                    "addedAt": "2026-05-05 09:30:00",
                    "sector": None,
                    "themes": "金风科技股份有限公司；4.30金风科技复盘：放量大涨，资金大幅流入 - 今日头条Loading.；金风科技最新消息，每天了解一只股票！",
                    "themeQuery": None,
                    "themeUpdatedAt": None,
                }
            ],
        )

        result = app.refresh_watchlist_profiles()
        rows = app.watchlist()
        watchlist_text = app.list_watchlist()

    assert "失败 0" in result
    assert rows[0]["sector"] == "电力设备-风电设备-风电整机"
    assert rows[0]["themes"] is None
    assert "复盘" not in watchlist_text
    assert "nan" not in watchlist_text


def test_alert_points_keep_lunch_break_flat():
    points = _normalize_points([("11:29", 10.0), ("11:30", 10.1), ("13:00", 10.4), ("13:01", 10.5)], 10.5)

    assert points == [("11:29", 10.0), ("11:30", 10.1), ("13:00", 10.1), ("13:01", 10.5)]
    assert _scale_trading_x("11:30", 0, 100) == _scale_trading_x("13:00", 0, 100)
    assert _scale_trading_x("14:12", 44, 650) < 694
    assert _scale_trading_x("14:12", 44, 650) > _scale_trading_x("14:00", 44, 650)


def test_jin10_json_rpc_parser_accepts_sse_events():
    raw = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'

    assert _parse_json_rpc(raw)["result"] == {"ok": True}


def test_jin10_json_rpc_parser_skips_non_json_sse_data():
    raw = ': ping\n\nevent: endpoint\ndata: /mcp\n\nevent: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"items":[]}}\n\n'

    assert _parse_json_rpc(raw)["id"] == 2


def test_jin10_json_rpc_batch_parser_accepts_sse_batch():
    raw = '\n'.join(
        [
            'event: message',
            'data: {"jsonrpc":"2.0","id":1,"result":{"server":"ok"}}',
            '',
            'event: message',
            'data: {"jsonrpc":"2.0","id":2,"result":{"structuredContent":{"data":{"items":[]}}}}',
        ]
    )

    parsed = _parse_json_rpc_batch(raw)

    assert [item["id"] for item in parsed] == [1, 2]
    assert parsed[1]["result"]["structuredContent"]["data"]["items"] == []


def test_jin10_json_rpc_parser_accepts_text_json_content():
    inner = json.dumps(
        {
            "data": {
                "has_more": True,
                "items": [
                    {
                        "content": "美国天然气期货",
                        "time": "2026-05-05T22:40:43+08:00",
                        "url": "https://flash.jin10.com/detail/1",
                    }
                ],
            }
        },
        ensure_ascii=False,
    )
    outer = {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": inner}]}}
    raw = "event: message\ndata: " + json.dumps(outer, ensure_ascii=False) + "\n\n"

    parsed = _parse_json_rpc(raw)
    content = _extract_jin10_structured_result(parsed["result"])

    assert content["data"]["items"][0]["content"] == "美国天然气期货"


def test_jin10_json_rpc_parser_accepts_split_sse_data():
    payload = json.dumps({"jsonrpc": "2.0", "id": 3, "result": {"ok": True}}, ensure_ascii=False)
    raw = f"event: message\ndata: {payload[:30]}\ndata: {payload[30:]}\n\n"

    assert _parse_json_rpc(raw)["result"] == {"ok": True}


def test_jin10_text_json_content_repairs_latin1_mojibake():
    original = "美国天然气期货"
    broken = original.encode("utf-8").decode("latin1")

    assert _repair_mojibake(broken) == original


def test_jin10_flash_page_helpers_use_data_wrapper_contract():
    page = {"data": {"items": [{"title": "快讯"}], "next_cursor": "cursor-1", "has_more": True}}

    assert _flash_page_items(page) == [{"title": "快讯"}]
    assert _flash_next_cursor(page) == "cursor-1"
    assert _flash_has_more(page) is True
