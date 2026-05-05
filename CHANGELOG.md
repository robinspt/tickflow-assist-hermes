# Changelog

## v0.3.9

- 迁移为 Hermes Python 插件：新增 `plugin.yaml`、Python `register(ctx)`、工具 schema、工具 handler 与 `/ta_` Slash Commands。
- 移除旧的 TypeScript / npm 插件入口与旧 manifest。
- 新增 Hermes 版一键安装脚本 `setup-tickflow.sh`，负责安装 Python 依赖、创建 `~/.hermes/plugins/tickflow-assist` 链接并生成本地配置。
- 一键安装默认创建项目内 `.venv`，避免 Debian/Ubuntu PEP 668 `externally-managed-environment` 阻止系统 pip 写入。
- 当项目目录或 `.venv` 不可写时，一键安装自动回退到 `~/.local/share/tickflow-assist-hermes/venv`，并支持 `TICKFLOW_ASSIST_VENV` 指定虚拟环境目录。
- 插件注册阶段改为 lazy-load 业务依赖，避免 `/plugins` 因 `pandas` 等工具运行时依赖未进入当前解释器路径而直接加载失败。
- 告警配置改为 Hermes delivery target 语义，使用 `alertDeliveryTarget` / `TICKFLOW_ASSIST_ALERT_DELIVERY_TARGET`。
- 文本告警通过 Hermes `send_message` 发送，PNG 告警卡通过 `MEDIA:/path/to/file` 附加。
- 定时日更改为创建 Hermes `cronjob` 任务：15:25 日更、20:00 收盘复盘。
- 保留原有 `/ta_` Slash Commands，包括 `/ta_backtest`、`/ta_refreshnames`、`/ta_refreshprofiles` 与 `/ta_debug`。
- 数据库继续使用 LanceDB，并保留原表名与字段：
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
- 保留原工具名，便于现有使用习惯迁移到 Hermes。
- 技术指标计算改为纯 Python/pandas/numpy 实现，不再依赖旧 Node bridge。
