---
name: database_query
description: Query TickFlow Assist LanceDB tables, schemas, and stored records through query_database.
metadata:
  hermes:
    plugin: tickflow-assist
---
# LanceDB 数据查询

查询 TickFlow Assist 数据库时必须使用 `query_database` 工具，不要直接读数据库文件。

映射：

- 数据库里有哪些表 -> `query_database(action="tables")`
- 看表结构 -> `query_database(action="schema", table="表名")`
- 查某只股票记录 -> `query_database(action="query", table="表名", symbol="002261")`
- 最近 N 条 -> `limit=N`
- 按字段排序 -> `sortBy` / `sortOrder`
- 关键词检索 -> `contains`

常用表：

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
