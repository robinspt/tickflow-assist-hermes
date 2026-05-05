---
name: ta-addstock
description: Add an A-share stock to TickFlow Assist watchlist. Args: SYMBOL [costPrice] [count].
metadata:
  hermes:
    plugin: tickflow-assist
---
# /ta-addstock

Parse args as `SYMBOL [costPrice] [count]`, call `add_stock`, and return the tool result `text` field.
