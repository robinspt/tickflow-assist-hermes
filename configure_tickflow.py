from __future__ import annotations

import getpass
import json
import sys
from pathlib import Path
from typing import Any


def main() -> int:
    config_file = Path(sys.argv[1])
    root_dir = Path(sys.argv[2])

    existing: dict[str, Any] = {}
    if config_file.exists():
        try:
            parsed = json.loads(config_file.read_text(encoding="utf-8"))
            existing = dict(parsed.get("plugin", parsed))
        except Exception:
            existing = {}

    interactive = sys.stdin.isatty()
    if interactive:
        print("请按提示填写配置；直接回车会沿用当前值或默认值。")
    else:
        print("WARN: 当前环境不是交互式 TTY，无法逐项提问；将使用已有配置或默认值。", file=sys.stderr)

    def prompt(key: str, label: str, default: str = "", secret: bool = False) -> str:
        current = str(existing.get(key) or "")
        fallback = current or default
        if not interactive:
            return fallback
        if secret:
            hint = "留空沿用当前值" if current else "留空跳过"
        else:
            hint = f"默认 {fallback}" if fallback else "留空跳过"
        message = f"{label}（{hint}）："
        value = getpass.getpass(message) if secret else input(message)
        return value.strip() or fallback

    def as_int(key: str, default: int) -> int:
        if not interactive:
            value = existing.get(key, default)
        else:
            raw = prompt(key, key, str(existing.get(key) or default))
            value = raw or default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def as_bool(key: str, default: bool) -> bool:
        current = existing.get(key)
        if not interactive:
            value = default if current is None else current
        else:
            default_text = "true" if (default if current is None else bool(current)) else "false"
            value = prompt(key, f"{key} true/false", default_text)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

    plugin = {
        "tickflowApiUrl": prompt("tickflowApiUrl", "TickFlow API 地址", "https://api.tickflow.org"),
        "tickflowApiKey": prompt("tickflowApiKey", "TickFlow API Key", secret=True),
        "tickflowApiKeyLevel": prompt("tickflowApiKeyLevel", "TickFlow Key 等级 Free/Starter/Pro/Expert", "Free"),
        "mxSearchApiUrl": prompt("mxSearchApiUrl", "妙想 Skills API 地址", "https://mkapi2.dfcfs.com/finskillshub/api/claw"),
        "mxSearchApiKey": prompt("mxSearchApiKey", "妙想 Skills API Key（可选）", secret=True),
        "jin10McpUrl": prompt("jin10McpUrl", "金十 MCP 地址", "https://mcp.jin10.com/mcp"),
        "jin10ApiToken": prompt("jin10ApiToken", "金十 API Token（可选）", secret=True),
        "jin10FlashPollInterval": as_int("jin10FlashPollInterval", 300),
        "jin10FlashRetentionDays": as_int("jin10FlashRetentionDays", 7),
        "jin10FlashNightAlert": as_bool("jin10FlashNightAlert", False),
        "llmBaseUrl": prompt("llmBaseUrl", "LLM Base URL", "https://api.openai.com/v1"),
        "llmApiKey": prompt("llmApiKey", "LLM API Key", secret=True),
        "llmModel": prompt("llmModel", "LLM Model", "gpt-4o"),
        "databasePath": prompt("databasePath", "LanceDB 数据库路径", "./data/lancedb"),
        "calendarFile": prompt("calendarFile", "交易日历文件", "./day_future.txt"),
        "requestInterval": as_int("requestInterval", 30),
        "dailyUpdateNotify": as_bool("dailyUpdateNotify", True),
        "alertDeliveryTarget": prompt(
            "alertDeliveryTarget",
            "Hermes delivery target（如 telegram / telegram:CHAT_ID / telegram:CHAT_ID:THREAD_ID / discord:CHANNEL_ID）",
            "telegram",
        ),
        "alertImageEnabled": as_bool("alertImageEnabled", True),
    }

    config_file.write_text(json.dumps({"plugin": plugin}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        label = str(config_file.relative_to(root_dir))
    except ValueError:
        label = str(config_file)
    print(f"已更新 {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
