---
name: ta-rmstock
description: Remove an A-share stock from TickFlow Assist watchlist. Args: SYMBOL.
metadata:
  hermes:
    plugin: tickflow-assist
---
# /ta-rmstock

Parse args as `SYMBOL`, call `remove_stock`, and return the tool result `text` field.
