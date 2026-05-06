# TickFlow Assist Hermes 使用指南

## 自然语言用法

在 Hermes 对话里可以直接说：

- `添加 002261`
- `添加 002261 成本 34.15`
- `查看自选`
- `分析 002261`
- `查看 002261 最近 3 次综合分析`
- `更新全部股票`
- `盘前资讯`
- `收盘复盘`
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
- `/ta_premarketbrief`
- `/ta_postclosereview`
- `/ta_dailyupdatestatus`
- `/ta_testalert`
- `/ta_screenstocks <自然语言选股条件>`
- `/ta_screenstocks_llm <自然语言选股条件>`
- `/ta_debug`

这些命令由 Hermes 插件 `register_command` 直接注册，handler 直接调用工具并返回 `text`，不会加载 skill，也不会走模型规划。`setup-tickflow.sh` 会清理旧的 `ta-*` / `ta_*` skill 链接，避免 Hermes chat 继续显示重复入口。

插件同时注册 `ta-*` 兼容别名，用于适配 Hermes gateway 在消息端分发插件命令时可能把下划线转换为连字符的查找逻辑；Telegram 菜单显示 `/ta_*`，实际执行不经过 skill。

Telegram 可从命令列表选择 `/ta_*` 并直接使用。Discord 目前不会在 `/` 菜单展示插件命令，但可以手动输入 `/ta_*` 或 `/ta-*`，例如 `/ta_watchlist`、`/ta-testalert`。如果消息端回复 `Unknown command /ta_watchlist`，说明 gateway 在进入插件前就拦截了该 slash command；请重启 gateway，并确认消息端 gateway 与 Hermes chat / CLI 使用同一个 Hermes profile / `$HOME`。

`/ta_debug` 不依赖 LanceDB 初始化成功也会输出诊断信息，包括 Hermes 当前 Python、虚拟环境记录、关键依赖导入状态和 Python 路径。若 `/plugins` 正常但其他命令提示缺少依赖，优先运行它确认 Hermes 是否加载到了安装脚本创建的虚拟环境。

## 本地调试

当前版本是 Hermes Python 插件，不再提供 npm CLI。可用 Hermes 工具调用，或在 Python 里直接导入：

```python
from tickflow_assist.tools import add_stock

print(add_stock({"symbol": "002261", "costPrice": 34.15}))
```

工具 handler 总是返回 JSON 字符串，主要文本在 `text` 字段。

## 监控与日更

`start_monitor` 会在 Hermes 进程内启动 daemon thread，用于交易时段高频轮询自选股报价与 `key_levels`。它也会发送阶段提醒：上午开盘、上午收盘、下午开盘、今日收盘各一次，并通过 `alert_log` 按上午盘/下午盘去重。如果 Hermes 重启时状态文件仍为运行中，插件加载后会自动恢复线程；`/ta_monitorstatus` 会显示后台线程是否存活、心跳是否超时以及最近异常。

Jin10 快讯监控在插件加载且 `jin10ApiToken` 已配置时自动启动 daemon thread。`/ta_flashstatus` 会显示后台轮询状态、轮询间隔、保留天数、关注列表、最近心跳、最近轮询、最近一轮入库/候选/告警、今日统计、续页补齐、最近清理、最近异常和最新快讯。

定时日更会在插件加载时默认启动 Hermes 进程内 daemon thread；`/ta_startdailyupdate` 可手动恢复，`/ta_stopdailyupdate` 会显式停用自动调度。线程按固定时间检查并执行：

- 盘前资讯：交易日 09:20 调用 `pre_market_brief`，窗口至 09:30；错过窗口后只记录跳过，不会在 15:25 前补跑阻塞日更。
- 日更：交易日 15:25 调用 `update_all`
- 复盘：交易日 20:00 调用 `post_close_review`，输出收盘复盘总览和每只自选股详情。

后台线程会直接调用插件工具，不经过 LLM、不消耗 token；如果 `dailyUpdateNotify=true`，会通过 `alertDeliveryTarget` 投递执行结果。收盘复盘会验证昨日活动关键位，给出明日沿用/微调/重算/暂停决策，结合已有金十快讯和行业资料生成新闻/板块段落，并把更新后的关键位写回 `key_levels` 与 `key_levels_history`。工具会写回 `daily-update-state.json`，因此 `/ta_dailyupdatestatus` 会分别显示盘前资讯、日更、复盘的最近尝试、最近成功、今日是否完成、失败摘要和最近投递异常。如果线程意外丢失，`/ta_dailyupdatestatus` 和 `/ta_*` 命令会尝试自动恢复未手动停用的调度线程；若 20:00 复盘曾因日更未完成而等待，后续日更成功后会继续补跑复盘。状态文件仍放在 `databasePath` 下，便于 `monitor_status` 和 `daily_update_status` 查看。

告警投递使用 `alertDeliveryTarget`，格式参考 Hermes delivery targets，例如 `telegram`、`telegram:-1001234567890`、`telegram:-1001234567890:17585`、`discord:999888777`。文本消息通过 Hermes `send_message` 发送；PNG 告警卡通过消息中的 `MEDIA:/path/to/file` 附加。

告警 PNG 版式复刻迁移前的 TickFlow 原版样式：市场涨跌背景、右上信号标签、左侧日内曲线、右侧关键价位和底部位阶带。Python 版会自动截断过长文本并错开位阶标签，避免文字重叠。
