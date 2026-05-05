---
name: ta-backtest
description: Backtest TickFlow key levels. Args: [SYMBOL] [recentLimit].
metadata:
  hermes:
    plugin: tickflow-assist
---
# /ta-backtest

If args are only a number, pass it as `recentLimit`. Otherwise parse args as `SYMBOL [recentLimit]`, call `backtest_key_levels`, and return the tool result `text` field.
