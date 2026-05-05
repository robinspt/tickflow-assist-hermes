from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tickflow_assist import schemas, tools
from tickflow_assist.alert_media import _normalize_points, _scale_trading_x
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
        if name == "cronjob":
            return json.dumps({"success": True, "job_id": f"job-{len(self.calls)}"})
        return json.dumps({"success": True})


def test_registers_all_declared_tools():
    ctx = DummyCtx()
    register(ctx)

    assert set(ctx.tools) == set(schemas.TOOL_SCHEMAS)
    assert ctx.tools["add_stock"]["toolset"] == "tickflow-assist"
    assert "pre_llm_call" in ctx.hooks
    assert set(ctx.commands) == {
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
        "ta_dailyupdatestatus",
        "ta_startdailyupdate",
        "ta_stopdailyupdate",
        "ta_testalert",
        "ta_screenstocks",
        "ta_screenstocks_llm",
        "ta_debug",
    }
    assert "ta-addstock" not in ctx.skills
    assert "debug_status" in ctx.tools


def test_command_handlers_return_text_field():
    ctx = DummyCtx()
    register(ctx)
    original = tools.HANDLERS["list_watchlist"]
    tools.HANDLERS["list_watchlist"] = lambda args: json.dumps({"ok": True, "text": "WATCHLIST"})
    try:
        assert ctx.commands["ta_watchlist"]["handler"]("") == "WATCHLIST"
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


def test_alert_delivery_target_uses_hermes_format_only():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "local.config.json").write_text(
            json.dumps({"plugin": {"alertDeliveryTarget": "telegram:-100123:456"}}),
            encoding="utf-8",
        )
        assert load_config(root).alert_delivery_target == "telegram:-100123:456"


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


def test_start_daily_update_uses_hermes_cron_jobs():
    with tempfile.TemporaryDirectory() as tmp:
        app = App(Config(database_path=tmp, alert_delivery_target="telegram"))
        ctx = DispatchCtx()
        app.set_context(ctx)
        result = app.start_daily_update()

    assert "Hermes cron" in result
    cron_calls = [args for name, args in ctx.calls if name == "cronjob"]
    assert [call["action"] for call in cron_calls] == ["create", "create"]
    assert [call["schedule"] for call in cron_calls] == ["25 15 * * 1-5", "0 20 * * 1-5"]
    assert all(call["deliver"] == "telegram" for call in cron_calls)


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
