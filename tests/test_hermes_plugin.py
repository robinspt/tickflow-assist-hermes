from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tickflow_assist import schemas
from tickflow_assist.config import Config, load_config
from tickflow_assist.core import App
from tickflow_assist.plugin import register
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
        "ta",
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
    assert "ta" in ctx.skills


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
