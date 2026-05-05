# TickFlow Assist Hermes

基于 Hermes 的 A 股监控与分析插件。插件使用 TickFlow API 获取行情与财务数据，继续使用原项目的 LanceDB 表结构保存自选、K 线、指标、分析结果、关键价位、告警和金十快讯记录。

最近更新：`v0.3.9` 迁移为 Hermes Python 插件，移除旧的运行时与 npm/TS 插件入口，数据库仍使用 LanceDB 且表名/字段保持不变。

## 架构

- Hermes 插件入口：`plugin.yaml` + `__init__.py`
- Python 负责插件注册、工具实现、TickFlow/MX/Jin10/LLM 调用与实时监控循环
- LanceDB 保持原表结构，不迁移为其他数据库
- 技术指标由 Python/pandas/numpy 计算
- 定时日更使用 Hermes `cronjob`
- 告警消息使用 Hermes `send_message`，PNG 图片通过 `MEDIA:/path/to/file` 附加

## 安装

推荐使用一键安装脚本：

```bash
git clone https://github.com/robinspt/tickflow-assist-hermes.git
cd tickflow-assist-hermes
./setup-tickflow.sh
```

脚本会完成以下工作：

- 优先使用 Hermes 自己的 Python 创建项目内 `.venv`，并执行 `.venv/bin/python -m pip install -e .`
- 写入 `.tickflow-assist-venv`，让 Hermes 运行时能找到实际虚拟环境依赖
- 将当前目录链接到 `~/.hermes/plugins/tickflow-assist`
- 清理本项目旧的 `~/.hermes/skills/ta*` 符号链接，避免继续通过 skill 触发
- 交互式生成或更新 `local.config.json`

如果项目目录不可写，脚本会自动改用 `~/.local/share/tickflow-assist-hermes/venv`。也可以显式指定：

```bash
TICKFLOW_ASSIST_VENV=~/.local/share/tickflow-assist-hermes/venv ./setup-tickflow.sh
```

手动安装也可以：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
mkdir -p ~/.hermes/plugins
ln -s "$(pwd)" ~/.hermes/plugins/tickflow-assist
```

如果曾安装过旧的 `/ta`、`/ta-*` 或 `/ta_*` skill 链接，重新运行脚本会清理指向本项目的旧符号链接，避免命令通过 skill 慢路径触发。

如果系统缺少 venv 支持，Debian/Ubuntu 可先执行 `sudo apt install python3-venv`。

重启 Hermes gateway 后，在 Hermes chat / CLI 里运行 `/plugins`，应能看到 `tickflow-assist`。Telegram / Discord 消息端可运行 `/commands` 查看 gateway 当前注册的命令；如果 `/ta_*` 不在列表里，说明 gateway 进程尚未加载插件命令，执行 `hermes gateway restart` 或在聊天里用 `/restart` 重启 gateway。Discord 全局命令有传播延迟，通常需要等待一小段时间。

如果已有 `.venv` 使用的 Python 版本与 Hermes 运行时不一致，脚本会自动把旧虚拟环境挪到 `.venv.pyX.Y.bak.*` 并重建。`/plugins` 正常但 `/ta_addstock` 提示缺少 `lancedb`、`pandas` 等依赖时，请重新运行 `./setup-tickflow.sh`，确认输出包含“Python 依赖检查通过”和“已记录虚拟环境路径”，然后重启 Hermes；仍失败时运行 `/ta_debug` 查看当前 Python、依赖和路径。

如果不使用 `local.config.json`，也可以配置环境变量：

```bash
export TICKFLOW_ASSIST_TICKFLOW_API_KEY="your-tickflow-key"
export TICKFLOW_ASSIST_TICKFLOW_API_KEY_LEVEL="Free"
export TICKFLOW_ASSIST_LLM_BASE_URL="https://api.openai.com/v1"
export TICKFLOW_ASSIST_LLM_API_KEY="sk-xxx"
export TICKFLOW_ASSIST_LLM_MODEL="gpt-4o"
```

可选：

```bash
export TICKFLOW_ASSIST_MX_SEARCH_API_KEY="mkt_xxx"
export TICKFLOW_ASSIST_JIN10_API_TOKEN="jin10_xxx"
export TICKFLOW_ASSIST_DATABASE_PATH="./data/lancedb"
export TICKFLOW_ASSIST_ALERT_DELIVERY_TARGET="telegram"
export TICKFLOW_ASSIST_ALERT_IMAGE_ENABLED="true"
```

也可以继续使用 `local.config.json` 的 `plugin` 字段，本仓库提供 [local.config.example.json](local.config.example.json)。

`alertDeliveryTarget` 使用 Hermes delivery target 格式，常见示例：

- `telegram`：发送到 Telegram home channel
- `telegram:-1001234567890`：发送到指定 Telegram 群
- `telegram:-1001234567890:17585`：发送到指定 Telegram topic
- `discord:999888777`：发送到指定 Discord channel
- `slack`：发送到 Slack home channel

## 工具

插件保留原工具名：

- 自选：`add_stock`、`remove_stock`、`list_watchlist`、`refresh_watchlist_names`
- 数据：`fetch_klines`、`fetch_intraday_klines`、`fetch_financials`、`update_all`
- 分析：`analyze`、`view_analysis`、`backtest_key_levels`
- 监控：`start_monitor`、`stop_monitor`、`monitor_status`
- 日更：`start_daily_update`、`stop_daily_update`、`daily_update_status`，内部创建/移除 Hermes cron jobs
- 数据库：`query_database`
- 妙想/东方财富：`mx_search`、`mx_data`、`mx_select_stock`、`screen_stock_candidates`、`list_eastmoney_watchlist`、`sync_eastmoney_watchlist`、`push_eastmoney_watchlist`、`remove_eastmoney_watchlist`
- 金十：`flash_monitor_status`
- 告警：`test_alert`

Hermes 中注册 `/ta_` 插件 Slash Commands，handler 直接调用工具并返回 `text`，不会加载 skill，也不会走模型规划。Hermes chat / CLI 可直接输入 `/ta_addstock`、`/ta_backtest`、`/ta_refreshnames`、`/ta_monitorstatus`、`/ta_testalert`、`/ta_debug`。Telegram / Discord 需要 gateway 的 `/commands` 列表中出现这些命令后才能用 `/ta_*` 触发。

如果 Telegram 回复 `Unknown command /ta_watchlist`，或者 Discord 原生 `/` 菜单没有显示插件命令，先用 `/commands` 确认 gateway 是否加载了插件命令。官方插件接口 `ctx.register_command()` 会把命令加入 CLI 和 gateway sessions；如果 Hermes chat / CLI 正常但消息端仍 unknown，通常是 gateway 进程未重启、运行的 Hermes 版本较旧，或消息端使用的是另一个 Hermes profile / `$HOME`。本项目不再用 skill 入口换取菜单展示。

## 数据库兼容

数据库路径默认是 `./data/lancedb`。Python 版通过 `lancedb` 和 `pyarrow` 直接读写原 LanceDB 表，保留以下表名与字段：

- `watchlist`
- `klines_daily`
- `klines_intraday`
- `indicators`
- `key_levels`
- `key_levels_history`
- `analysis_log`
- `technical_analysis`
- `financial_analysis`
- `news_analysis`
- `composite_analysis`
- `alert_log`
- `jin10_flash`
- `jin10_flash_delivery`
- `universes`
- `universe_memberships`

不会自动改表、扩表或迁移到 SQLite。

## 验证

```bash
python -m compileall __init__.py schemas.py tools.py tickflow_assist
python -c "import importlib.util; spec=importlib.util.spec_from_file_location('t','tests/test_hermes_plugin.py'); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); m.test_registers_all_declared_tools(); m.test_lancedb_schema_keeps_existing_fields(); print('ok')"
```

如果安装了 pytest，也可以运行：

```bash
python -m pytest
```

## 风险提示

本项目仅用于策略研究、流程验证与教学交流，不构成投资建议、收益承诺或具体交易指引。

## License

MIT
