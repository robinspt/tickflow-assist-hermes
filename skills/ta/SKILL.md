---
name: ta
description: TickFlow Assist command router for Discord and Telegram. Use with args like "addstock 002202", "analyze 002202", "watchlist", "monitorstatus", "testalert", or "debug".
metadata:
  hermes:
    plugin: tickflow-assist
---
# TickFlow Assist 命令入口

当用户通过 `/ta` 传入参数时，把参数解析为 TickFlow Assist 操作并调用对应工具。工具返回 JSON 时，优先原样返回 `text` 字段。

命令映射：

- `addstock 002202 [costPrice] [count]`、`add 002202` -> `add_stock`
- `rmstock 002202`、`remove 002202` -> `remove_stock`
- `watchlist`、`list` -> `list_watchlist`
- `analyze 002202` -> `analyze`
- `backtest 002202 [recentLimit]` -> `backtest_key_levels`
- `viewanalysis 002202`、`view 002202` -> `view_analysis`
- `monitorstatus`、`monitor` -> `monitor_status`
- `flashstatus`、`flash` -> `flash_monitor_status`
- `startmonitor` -> `start_monitor`
- `stopmonitor` -> `stop_monitor`
- `updateall`、`update` -> `update_all`
- `dailyupdatestatus`、`daily` -> `daily_update_status`
- `startdailyupdate`、`startdaily` -> `start_daily_update`
- `stopdailyupdate`、`stopdaily` -> `stop_daily_update`
- `testalert`、`test` -> `test_alert`
- `screenstocks <条件>`、`screen <条件>` -> `screen_stock_candidates`
- `screenstocks_llm <条件>`、`screenllm <条件>` -> `screen_stock_candidates`，并设置 `summarize=true`

规则：

- 不要臆造股票代码或参数。
- 如果参数不足，简短提示正确用法。
- 如果用户输入 `debug`，提示使用 `/ta_debug` 或 `/ta debug` 查看插件诊断。
