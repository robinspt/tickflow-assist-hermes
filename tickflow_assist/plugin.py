from __future__ import annotations

import json
from pathlib import Path

from . import schemas, tools

COMMAND_MAP = {
    "ta_addstock": ("add_stock", lambda s: {"symbol": (s.split() + [""])[0], "costPrice": _part(s, 1), "count": _part(s, 2)}),
    "ta_rmstock": ("remove_stock", lambda s: {"symbol": s.strip()}),
    "ta_analyze": ("analyze", lambda s: {"symbol": s.strip()}),
    "ta_backtest": ("backtest_key_levels", lambda s: _parse_backtest_args(s)),
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
    "ta_debug": ("debug_status", lambda s: {}),
}


def _pre_llm_context(**kwargs):
    user_message = str(kwargs.get("user_message") or "")
    command_context = _command_fallback_context(user_message)
    base = (
        "TickFlow Assist Hermes 插件可用于A股自选、日K/分钟K、分析、监控、日更、"
        "LanceDB 查询、妙想搜索/选股和东方财富自选同步。"
        "涉及这些意图时优先调用 tickflow-assist 工具；工具返回 JSON 中的 text 字段应尽量原样转述。"
    )
    return {"context": base + ("\n\n" + command_context if command_context else "")}


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
    try:
        app = tools.get_app()
        if app.jin10.configured():
            app.start_flash_monitor()
    except Exception:
        pass


def _register_commands(ctx) -> None:
    for command, (tool_name, parser) in COMMAND_MAP.items():
        handler = _make_command_handler(command)
        ctx.register_command(command, handler=handler, description=f"TickFlow Assist {tool_name}")
        alias = command.replace("_", "-")
        if alias != command:
            ctx.register_command(alias, handler=handler, description=f"TickFlow Assist {tool_name}")


def _make_command_handler(command: str):
    def handler(raw_args: str) -> str:
        return _run_command_text(command, raw_args or "")

    return handler


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


def _resolve_ta_command(raw_args: str) -> tuple[str, str] | None:
    text = (raw_args or "").strip()
    if not text:
        return None
    first, _, rest = text.partition(" ")
    normalized = first.strip().lstrip("/").replace("-", "_").lower()
    if normalized in COMMAND_MAP or normalized == "ta_debug":
        return normalized, rest
    return None


def _run_command_text(command: str, args: str) -> str:
    tool_name, parser = COMMAND_MAP[command]
    return _json_text_field(tools.HANDLERS[tool_name](parser(args or "")))


def _json_text_field(payload: str) -> str:
    try:
        parsed = json.loads(payload)
    except Exception:
        return payload
    if isinstance(parsed, dict):
        text = parsed.get("text")
        if text:
            return str(text)
        error = parsed.get("error")
        if error:
            return f"⚠️ {error}"
    return payload


def _command_fallback_context(user_message: str) -> str:
    text = user_message.strip()
    if text.startswith("/ta ") or text == "/ta":
        return "用户输入了 TickFlow Assist 总入口形式。请提示用户直接选择或输入 `/ta_testalert`、`/ta_addstock`、`/ta_analyze`、`/ta_watchlist`、`/ta_monitorstatus`、`/ta_debug` 等独立命令。"
    if not (text.startswith("/ta_") or text.startswith("ta_") or text.startswith("/ta-") or text.startswith("ta-")):
        return ""
    raw = text[1:] if text.startswith("/") else text
    parsed = _resolve_ta_command(raw)
    if parsed is None:
        return (
            "用户输入的是 TickFlow Assist 命令格式。请引导用户直接选择或输入 `/ta_addstock`、"
            "`/ta_analyze`、`/ta_watchlist`、`/ta_monitorstatus`、`/ta_testalert` 或 `/ta_debug`。"
        )
    command, args = parsed
    tool_name, parser = COMMAND_MAP[command]
    tool_args = parser(args)
    return (
        "用户消息是 TickFlow Assist slash command，但当前平台可能没有原生分发。"
        f"请直接调用工具 `{tool_name}`，参数为 `{json.dumps(tool_args, ensure_ascii=False)}`。"
        "最终回复应优先原样返回工具结果 JSON 的 `text` 字段。"
    )
