---
name: stock_analysis
description: Use TickFlow Assist Hermes tools for A-share watchlist, K-lines, analysis, monitoring, LanceDB query, MX search, and Eastmoney watchlist workflows.
metadata:
  hermes:
    plugin: tickflow-assist
---
# 股票分析与监控

优先通过 TickFlow Assist Hermes 插件工具完成 A 股相关任务，并尽量原样输出工具返回 JSON 中的 `text` 字段。

常用意图映射：

- 添加自选、加入观察、加股票 -> `add_stock`
- 删除自选、移除股票 -> `remove_stock`
- 查看自选、自选列表 -> `list_watchlist`
- 分析股票、分析 002261 -> `analyze`
- 查看上次分析、最近 N 次分析 -> `view_analysis`
- 拉日K、更新日线 -> `fetch_klines`
- 拉分钟K、获取 1m 分钟线 -> `fetch_intraday_klines`
- 全部更新、执行日更 -> `update_all`
- 盘前资讯、开盘前资讯简报 -> `pre_market_brief`
- 收盘复盘、盘后复盘 -> `post_close_review`
- 开始盯盘、启动监控 -> `start_monitor`
- 停止盯盘、停止监控 -> `stop_monitor`
- 监控状态 -> `monitor_status`
- 启动定时日更 -> `start_daily_update`
- 停止定时日更 -> `stop_daily_update`
- 日更状态 -> `daily_update_status`
- 数据库表、表结构、查询记录 -> `query_database`
- 搜资讯、公告、研报、政策、事件解读 -> `mx_search`
- 查行情、财务、资金、股东、高管、公司信息 -> `mx_data`
- 自然语言选股 -> `mx_select_stock`
- 选股并补数据、候选池 -> `screen_stock_candidates`
- 查看东方财富自选 -> `list_eastmoney_watchlist`
- 同步东方财富自选到本地 -> `sync_eastmoney_watchlist`
- 推送本地自选到东方财富 -> `push_eastmoney_watchlist`
- 删除东方财富自选 -> `remove_eastmoney_watchlist`
- 金十快讯状态 -> `flash_monitor_status`
- 测试告警 -> `test_alert`

规则：

- 股票代码按用户原始输入提取，例如 `002261`。
- 成本价对应 `costPrice`；若用户未提供成本价，可省略。
- 用户提到“N天日K”时，对应 `count`。
- `start_daily_update` 创建 09:20 盘前资讯、15:25 日更、20:00 收盘复盘三个 Hermes cron 任务；不要把它和一次性的 `update_all` 混淆。
- 新闻、公告、研报、政策和事件类问题优先用 `mx_search`。
- 官方数据、行情、财务和公司信息类问题优先用 `mx_data`。
- 自然语言找股票优先用 `mx_select_stock`；需要候选池和补数据时用 `screen_stock_candidates`。
- 数据库查询必须用 `query_database`，不要绕过工具直接读 LanceDB 文件。
- 不要臆造股票代码、成本价、日期、阈值、分析结果或监控状态。
- 工具成功时直接返回结果；工具报错时保留错误原文。
