from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from tickflow_assist import schemas, tools
from tickflow_assist.alert_media import _normalize_points, _scale_trading_x
from tickflow_assist.clients import _extract_jin10_structured_result, _parse_json_rpc, _parse_json_rpc_batch, _repair_mojibake
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
        if name == "cronjob":
            return json.dumps({"success": True, "job_id": f"job-{len(self.calls)}"})
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
    assert [call["action"] for call in cron_calls] == ["create", "create", "create"]
    assert [call["schedule"] for call in cron_calls] == ["20 9 * * 1-5", "25 15 * * 1-5", "0 20 * * 1-5"]
    assert all(call["deliver"] == "telegram" for call in cron_calls)
    assert all(call["skills"] == ["stock-analysis"] for call in cron_calls)


def test_start_daily_update_migrates_old_two_job_schedule():
    with tempfile.TemporaryDirectory() as tmp:
        app = App(Config(database_path=tmp, alert_delivery_target="telegram"))
        ctx = DispatchCtx()
        app.set_context(ctx)
        app._write_daily_state({"running": True, "scheduleVersion": 1, "jobIds": ["old-daily", "old-review"]})

        result = app.start_daily_update()
        state = app._read_daily_state()

    cron_calls = [args for name, args in ctx.calls if name == "cronjob"]
    assert [call["action"] for call in cron_calls] == ["remove", "remove", "create", "create", "create"]
    assert state["scheduleVersion"] == 2
    assert len(state["jobIds"]) == 3
    assert "已移除旧任务" in result


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
