# TickFlow Assist Hermes 使用指南

## 自然语言用法

在 Hermes 对话里可以直接说：

- `添加 002261`
- `添加 002261 成本 34.15`
- `查看自选`
- `分析 002261`
- `查看 002261 最近 3 次综合分析`
- `更新全部股票`
- `开始监控`
- `监控状态`
- `启动定时日更`
- `TickFlow日更状态`
- `搜索立讯精密最新研报`
- `找今日涨幅 2% 的股票`
- `数据库里有哪些表`

插件通过 `pre_llm_call` hook 提示 Hermes 优先使用 TickFlow Assist 工具。

## Slash Commands

常用命令：

- `/ta-addstock <symbol> [costPrice] [count]`
- `/ta-rmstock <symbol>`
- `/ta-watchlist`
- `/ta-analyze <symbol>`
- `/ta-backtest [symbol] [recentLimit]`
- `/ta-viewanalysis <symbol>`
- `/ta-refreshnames`
- `/ta-refreshprofiles [symbol]`
- `/ta-startmonitor`
- `/ta-stopmonitor`
- `/ta-monitorstatus`
- `/ta-flashstatus`
- `/ta-startdailyupdate`
- `/ta-stopdailyupdate`
- `/ta-updateall`
- `/ta-dailyupdatestatus`
- `/ta-testalert`
- `/ta-screenstocks <自然语言选股条件>`
- `/ta-screenstocks-llm <自然语言选股条件>`
- `/ta-debug`

这些命令由 Hermes skills 生成，统一使用 `/ta-*` 形式。本项目不再注册第二套 `/ta_` 插件命令，避免 Hermes chat 中重复显示。Discord 中这些命令应作为可选择的原生 slash commands 出现在 `/` 菜单；前提是 `setup-tickflow.sh` 已把 `ta-*` skills 链接到 `~/.hermes/skills`，bot 邀请时包含 `applications.commands` scope，`DISCORD_COMMAND_SYNC_POLICY` 没有设为 `off`，并且 gateway 已在插件更新后重启。Telegram 重启 gateway 后会刷新 bot 命令菜单。

`/ta-debug` 不依赖 LanceDB 初始化成功也会输出诊断信息，包括 Hermes 当前 Python、虚拟环境记录、关键依赖导入状态和 Python 路径。若 `/plugins` 正常但其他命令提示缺少依赖，优先运行它确认 Hermes 是否加载到了安装脚本创建的虚拟环境。

## 本地调试

当前版本是 Hermes Python 插件，不再提供 npm CLI。可用 Hermes 工具调用，或在 Python 里直接导入：

```python
from tickflow_assist.tools import add_stock

print(add_stock({"symbol": "002261", "costPrice": 34.15}))
```

工具 handler 总是返回 JSON 字符串，主要文本在 `text` 字段。

## 监控与日更

`start_monitor` 会在 Hermes 进程内启动 daemon thread，用于交易时段高频轮询自选股报价与 `key_levels`。

`start_daily_update` 会创建 Hermes cron jobs：

- 日更：交易日 15:25 调用 `update_all`
- 复盘：交易日 20:00 对自选股调用 `analyze`

状态文件仍放在 `databasePath` 下，便于 `monitor_status` 和 `daily_update_status` 查看。

告警投递使用 `alertDeliveryTarget`，格式参考 Hermes delivery targets，例如 `telegram`、`telegram:-1001234567890`、`telegram:-1001234567890:17585`、`discord:999888777`。文本消息通过 Hermes `send_message` 发送；PNG 告警卡通过消息中的 `MEDIA:/path/to/file` 附加。

告警 PNG 版式复刻迁移前的 TickFlow 原版样式：市场涨跌背景、右上信号标签、左侧日内曲线、右侧关键价位和底部位阶带。Python 版会自动截断过长文本并错开位阶标签，避免文字重叠。
