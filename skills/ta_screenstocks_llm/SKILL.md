---
name: ta_screenstocks_llm
description: Screen A-share stock candidates and summarize with LLM. Args: natural language criteria.
metadata:
  hermes:
    plugin: tickflow-assist
---
# /ta_screenstocks_llm

Pass all args as `keyword` to `screen_stock_candidates` with `summarize=true`, and return the tool result `text` field.
