from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int(value: Any, default: int, minimum: int = 1) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, parsed)


def _level(value: str) -> str:
    normalized = (value or "free").strip().lower()
    aliases = {"start": "starter"}
    return aliases.get(normalized, normalized if normalized in {"free", "starter", "pro", "expert"} else "free")


@dataclass(frozen=True)
class Config:
    tickflow_api_url: str = "https://api.tickflow.org"
    tickflow_api_key: str = ""
    tickflow_api_key_level: str = "free"
    mx_search_api_url: str = "https://mkapi2.dfcfs.com/finskillshub/api/claw"
    mx_search_api_key: str = ""
    jin10_mcp_url: str = "https://mcp.jin10.com/mcp"
    jin10_api_token: str = ""
    jin10_flash_poll_interval: int = 300
    jin10_flash_retention_days: int = 7
    jin10_flash_night_alert: bool = False
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o"
    database_path: str = "./data/lancedb"
    calendar_file: str = "./day_future.txt"
    request_interval: int = 30
    daily_update_notify: bool = True
    alert_delivery_target: str = ""
    alert_image_enabled: bool = True


def load_config(base_dir: Path | None = None) -> Config:
    root = base_dir or Path.cwd()
    raw: dict[str, Any] = {}
    local = root / "local.config.json"
    if local.exists():
        try:
            parsed = json.loads(local.read_text(encoding="utf-8"))
            raw = dict(parsed.get("plugin", parsed))
        except Exception:
            raw = {}

    def val(camel: str, *envs: str, default: Any = "") -> Any:
        if raw.get(camel) not in (None, ""):
            return raw[camel]
        return _first_env(*envs, default=str(default))

    cfg = Config(
        tickflow_api_url=str(val("tickflowApiUrl", "TICKFLOW_ASSIST_TICKFLOW_API_URL", "TICKFLOW_API_URL", default="https://api.tickflow.org")),
        tickflow_api_key=str(val("tickflowApiKey", "TICKFLOW_ASSIST_TICKFLOW_API_KEY", "TICKFLOW_API_KEY")),
        tickflow_api_key_level=_level(str(val("tickflowApiKeyLevel", "TICKFLOW_ASSIST_TICKFLOW_API_KEY_LEVEL", "TICKFLOW_API_KEY_LEVEL", default="free"))),
        mx_search_api_url=str(val("mxSearchApiUrl", "TICKFLOW_ASSIST_MX_SEARCH_API_URL", "MX_SEARCH_API_URL", default="https://mkapi2.dfcfs.com/finskillshub/api/claw")),
        mx_search_api_key=str(val("mxSearchApiKey", "TICKFLOW_ASSIST_MX_SEARCH_API_KEY", "MX_SEARCH_API_KEY", "MX_APIKEY")),
        jin10_mcp_url=str(val("jin10McpUrl", "TICKFLOW_ASSIST_JIN10_MCP_URL", "JIN10_MCP_URL", default="https://mcp.jin10.com/mcp")),
        jin10_api_token=str(val("jin10ApiToken", "TICKFLOW_ASSIST_JIN10_API_TOKEN", "JIN10_API_TOKEN")),
        jin10_flash_poll_interval=_as_int(raw.get("jin10FlashPollInterval"), 300, 10),
        jin10_flash_retention_days=_as_int(raw.get("jin10FlashRetentionDays"), 7, 1),
        jin10_flash_night_alert=_as_bool(raw.get("jin10FlashNightAlert"), False),
        llm_base_url=str(val("llmBaseUrl", "TICKFLOW_ASSIST_LLM_BASE_URL", "LLM_BASE_URL", default="https://api.openai.com/v1")),
        llm_api_key=str(val("llmApiKey", "TICKFLOW_ASSIST_LLM_API_KEY", "LLM_API_KEY")),
        llm_model=str(val("llmModel", "TICKFLOW_ASSIST_LLM_MODEL", "LLM_MODEL", default="gpt-4o")),
        database_path=str(raw.get("databasePath") or _first_env("TICKFLOW_ASSIST_DATABASE_PATH", default="./data/lancedb")),
        calendar_file=str(raw.get("calendarFile") or _first_env("TICKFLOW_ASSIST_CALENDAR_FILE", default="./day_future.txt")),
        request_interval=_as_int(raw.get("requestInterval"), 30, 5),
        daily_update_notify=_as_bool(raw.get("dailyUpdateNotify"), True),
        alert_delivery_target=str(raw.get("alertDeliveryTarget") or _first_env("TICKFLOW_ASSIST_ALERT_DELIVERY_TARGET")),
        alert_image_enabled=_as_bool(raw.get("alertImageEnabled") if "alertImageEnabled" in raw else _first_env("TICKFLOW_ASSIST_ALERT_IMAGE_ENABLED"), True),
    )
    return Config(
        **{
            **cfg.__dict__,
            "database_path": _resolve_path(cfg.database_path, root),
            "calendar_file": _resolve_path(cfg.calendar_file, root),
        }
    )


def _resolve_path(value: str, root: Path) -> str:
    if not value:
        return value
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else root / path)


def supports_intraday(level: str) -> bool:
    return level in {"pro", "expert"}


def supports_financial(level: str) -> bool:
    return level == "expert"
