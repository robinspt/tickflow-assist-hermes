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

- `/ta_addstock <symbol> [costPrice] [count]`
- `/ta_rmstock <symbol>`
- `/ta_watchlist`
- `/ta_analyze <symbol>`
- `/ta_backtest [symbol] [recentLimit]`
- `/ta_viewanalysis <symbol>`
- `/ta_refreshnames`
- `/ta_refreshprofiles [symbol]`
- `/ta_startmonitor`
- `/ta_stopmonitor`
- `/ta_monitorstatus`
- `/ta_flashstatus`
- `/ta_startdailyupdate`
- `/ta_stopdailyupdate`
- `/ta_updateall`
- `/ta_dailyupdatestatus`
- `/ta_testalert`
- `/ta_screenstocks <自然语言选股条件>`
- `/ta_screenstocks_llm <自然语言选股条件>`
- `/ta_debug`

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
