from __future__ import annotations

import json
from pathlib import Path

from . import schemas, tools
from .config import load_config


def _pre_llm_context(**kwargs):
    return {
        "context": (
            "TickFlow Assist Hermes 插件可用于A股自选、日K/分钟K、分析、监控、日更、"
            "LanceDB 查询、妙想搜索/选股和东方财富自选同步。"
            "涉及这些意图时优先调用 tickflow-assist 工具；工具返回 JSON 中的 text 字段应尽量原样转述。"
        )
    }


def register(ctx):
    tools.set_context(ctx)
    for name, schema in schemas.TOOL_SCHEMAS.items():
        ctx.register_tool(name=name, toolset="tickflow-assist", schema=schema, handler=tools.HANDLERS[name])
    skills_dir = Path(__file__).resolve().parents[1] / "skills"
    if skills_dir.exists():
        for child in sorted(skills_dir.iterdir()):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.exists():
                ctx.register_skill(child.name, skill_md)
    ctx.register_hook("pre_llm_call", _pre_llm_context)
    _register_commands(ctx)


def _register_commands(ctx) -> None:
    command_map = {
        "ta_addstock": ("add_stock", lambda s: {"symbol": (s.split() + [""])[0], "costPrice": _part(s, 1), "count": _part(s, 2)}),
        "ta_rmstock": ("remove_stock", lambda s: {"symbol": s.strip()}),
        "ta_analyze": ("analyze", lambda s: {"symbol": s.strip()}),
        "ta_backtest": ("backtest_key_levels", _parse_backtest_args),
        "ta_viewanalysis": ("view_analysis", lambda s: {"symbol": (s.split() + [""])[0]}),
        "ta_watchlist": ("list_watchlist", lambda s: {}),
        "ta_refreshnames": ("refresh_watchlist_names", lambda s: {}),
        "ta_refreshprofiles": ("refresh_watchlist_profiles", lambda s: {"symbol": s.strip() or None}),
        "ta_monitorstatus": ("monitor_status", lambda s: {}),
        "ta_flashstatus": ("flash_monitor_status", lambda s: {}),
        "ta_startmonitor": ("start_monitor", lambda s: {}),
        "ta_stopmonitor": ("stop_monitor", lambda s: {}),
        "ta_updateall": ("update_all", lambda s: {}),
        "ta_dailyupdatestatus": ("daily_update_status", lambda s: {}),
        "ta_startdailyupdate": ("start_daily_update", lambda s: {}),
        "ta_stopdailyupdate": ("stop_daily_update", lambda s: {}),
        "ta_testalert": ("test_alert", lambda s: {}),
        "ta_screenstocks": ("screen_stock_candidates", lambda s: {"keyword": s.strip()}),
        "ta_screenstocks_llm": ("screen_stock_candidates", lambda s: {"keyword": s.strip(), "summarize": True}),
    }
    for command, (tool_name, parser) in command_map.items():
        def handler(raw_args, _tool_name=tool_name, _parser=parser):
            return tools.HANDLERS[_tool_name](_parser(raw_args or ""))
        ctx.register_command(command, handler=handler, description=f"TickFlow Assist {tool_name}")
    ctx.register_command("ta_debug", handler=lambda raw_args: _debug_status(), description="TickFlow Assist debug status")


def _part(text: str, index: int):
    parts = text.split()
    return parts[index] if len(parts) > index else None


def _parse_backtest_args(text: str) -> dict:
    parts = text.split()
    if not parts:
        return {}
    if len(parts) == 1 and parts[0].isdigit():
        return {"recentLimit": parts[0]}
    return {"symbol": parts[0], "recentLimit": parts[1] if len(parts) > 1 else None}


def _debug_status() -> str:
    diagnostics = tools.runtime_diagnostics()
    lines = [
        "🛠 TickFlow 调试信息",
        "运行方式: Hermes Python plugin",
        f"Python: {diagnostics['python']}",
        f"插件目录: {diagnostics['root']}",
        f"虚拟环境记录: {diagnostics['venv_marker'] or '未记录'}",
        "依赖状态:",
    ]
    for item in diagnostics["dependencies"]:
        if item["ok"]:
            version = f" {item['version']}" if item.get("version") else ""
            origin = f" @ {item['origin']}" if item.get("origin") else ""
            lines.append(f"  ✅ {item['module']}{version}{origin}")
        else:
            lines.append(f"  ❌ {item['module']}: {item['error']}")

    if diagnostics["paths"]:
        lines.append("Python 路径:")
        lines.extend(f"  {path}" for path in diagnostics["paths"])

    try:
        cfg = load_config(Path(__file__).resolve().parents[1])
        lines.extend(
            [
                f"数据库路径: {cfg.database_path}",
                f"数据库目录存在: {'是' if Path(cfg.database_path).expanduser().exists() else '否'}",
                f"交易日历: {cfg.calendar_file}",
                f"轮询间隔: {cfg.request_interval}",
                f"alertDeliveryTarget: {cfg.alert_delivery_target or '未配置'}",
            ]
        )
    except Exception as exc:
        lines.append(f"配置读取: 失败：{exc}")
    return json.dumps({"ok": True, "text": "\n".join(lines)}, ensure_ascii=False)
