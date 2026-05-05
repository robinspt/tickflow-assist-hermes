from __future__ import annotations

import json
import time
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib.parse import urljoin

import requests

from .config import Config
from .utils import as_list, first_nonempty, get_nested, normalize_symbol, safe_float


class TickFlowClient:
    def __init__(self, cfg: Config):
        self.base_url = cfg.tickflow_api_url.rstrip("/") + "/"
        self.api_key = cfg.tickflow_api_key

    def _request(self, method: str, path: str, **kwargs) -> Any:
        headers = kwargs.pop("headers", {})
        headers.update({"x-api-key": self.api_key, "Content-Type": "application/json"})
        url = urljoin(self.base_url, path.lstrip("/"))
        response = requests.request(method, url, headers=headers, timeout=45, **kwargs)
        if response.status_code == 429:
            time.sleep(float(response.headers.get("Retry-After", "5")))
            response = requests.request(method, url, headers=headers, timeout=45, **kwargs)
        if not response.ok:
            raise RuntimeError(f"TickFlow request failed: {response.status_code} {response.text}")
        return response.json()

    def instruments(self, symbols: list[str]) -> list[dict[str, Any]]:
        if not symbols:
            return []
        payload = self._request("GET", "/v1/instruments", params={"symbols": ",".join(symbols)})
        return list(payload.get("data") or [])

    def quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        if not symbols:
            return []
        payload = self._request("POST", "/v1/quotes", json={"symbols": symbols})
        return list(payload.get("data") or [])

    def klines(self, symbol: str, count: int = 90, period: str = "1d", adjust: str = "forward") -> list[dict[str, Any]]:
        symbol = normalize_symbol(symbol)
        payload = self._request(
            "GET",
            "/v1/klines/batch",
            params={"symbols": symbol, "period": period, "count": count, "adjust": adjust},
        )
        compact = (payload.get("data") or {}).get(symbol)
        return _compact_to_rows(symbol, compact, period)

    def intraday(self, symbol: str, count: int = 240, period: str = "1m") -> list[dict[str, Any]]:
        symbol = normalize_symbol(symbol)
        payload = self._request(
            "GET",
            "/v1/klines/intraday/batch",
            params={"symbols": symbol, "period": period, "count": count},
        )
        compact = (payload.get("data") or {}).get(symbol)
        return _compact_to_rows(symbol, compact, period)

    def financial_snapshot(self, symbol: str, latest: int = 4) -> dict[str, Any]:
        symbol = normalize_symbol(symbol)
        sections = {
            "income": "/v1/financials/income",
            "metrics": "/v1/financials/metrics",
            "cashFlow": "/v1/financials/cash-flow",
            "balanceSheet": "/v1/financials/balance-sheet",
        }
        output = {"symbol": symbol}
        for key, path in sections.items():
            payload = self._request("GET", path, params={"symbols": symbol, "latest": latest})
            output[key] = (payload.get("data") or {}).get(symbol) or []
        return output

    def list_universes(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/v1/universes")
        return list(payload.get("data") or [])

    def universe(self, universe_id: str) -> dict[str, Any] | None:
        universe_id = str(universe_id or "").strip()
        if not universe_id:
            return None
        payload = self._request("GET", f"/v1/universes/{universe_id}")
        return payload.get("data")

    def universe_batch(self, universe_ids: list[str]) -> dict[str, dict[str, Any]]:
        ids = [str(item or "").strip() for item in universe_ids if str(item or "").strip()]
        if not ids:
            return {}
        payload = self._request("POST", "/v1/universes/batch", json={"ids": ids})
        return dict(payload.get("data") or {})


class MxClient:
    def __init__(self, cfg: Config):
        self.base_url = cfg.mx_search_api_url.rstrip("/")
        self.api_key = cfg.mx_search_api_key

    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def post(self, endpoint: str, body: dict[str, Any]) -> Any:
        if not self.configured():
            raise RuntimeError("MX API 未配置，请设置 mxSearchApiKey / TICKFLOW_ASSIST_MX_SEARCH_API_KEY")
        base = self.base_url
        for suffix in ["news-search", "stock-screen", "query", "self-select/get", "self-select/manage"]:
            if base.endswith("/" + suffix):
                base = base[: -(len(suffix) + 1)]
        response = requests.post(f"{base}/{endpoint}", headers={"Content-Type": "application/json", "apikey": self.api_key}, json=body, timeout=45)
        if not response.ok:
            raise RuntimeError(f"MX request failed: {response.status_code} {response.text}")
        return response.json()

    def search(self, query: str) -> list[dict[str, Any]]:
        return _normalize_documents(self.post("news-search", {"query": query}))

    def data(self, query: str) -> dict[str, Any]:
        return self.post("query", {"toolQuery": query})

    def select(self, keyword: str, page_size: int = 20) -> dict[str, Any]:
        return self.post("stock-screen", {"keyword": keyword, "pageNo": 1, "pageSize": page_size})

    def eastmoney_watchlist(self) -> Any:
        return self.post("self-select/get", {})

    def manage_watchlist(self, query: str) -> Any:
        return self.post("self-select/manage", {"query": query})


class Jin10Client:
    def __init__(self, cfg: Config):
        self.url = cfg.jin10_mcp_url
        self.token = cfg.jin10_api_token
        self.session_id: str | None = None
        self.request_id = 1
        self.initialized = False

    def configured(self) -> bool:
        return bool(self.url and self.token)

    def list_flash(self, cursor: str | None = None) -> dict[str, Any]:
        if not self.configured():
            raise RuntimeError("Jin10 MCP 未配置，请设置 jin10ApiToken")
        if not self.initialized:
            self._initialize()
        return self._call_tool("list_flash", {"cursor": cursor} if cursor else {})

    def _initialize(self) -> None:
        self._request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "tickflow-assist-hermes", "version": "0.3.9"}})
        self._notify("notifications/initialized")
        self.initialized = True

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise RuntimeError(f"jin10 tool {name} returned error")
        if "structuredContent" in result:
            return result["structuredContent"]
        for item in result.get("content") or []:
            if "structuredContent" in item:
                return item["structuredContent"]
            if item.get("text"):
                try:
                    return json.loads(item["text"])
                except Exception:
                    return {"text": item["text"]}
        return result

    def _request(self, method: str, params: Any) -> Any:
        payload = {"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params}
        self.request_id += 1
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.token}", "X-API-Key": self.token, "Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        response = requests.post(self.url, headers=headers, json=payload, timeout=45)
        if response.headers.get("mcp-session-id"):
            self.session_id = response.headers["mcp-session-id"]
        if not response.ok:
            raise RuntimeError(f"jin10 MCP request failed: {response.status_code} {response.text}")
        parsed = _parse_json_rpc(response.text)
        if parsed.get("error"):
            raise RuntimeError(f"jin10 MCP error: {parsed['error']}")
        return parsed.get("result")

    def _notify(self, method: str) -> None:
        requests.post(self.url, headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}", "X-API-Key": self.token}, json={"jsonrpc": "2.0", "method": method}, timeout=20)


def call_llm(cfg: Config, system: str, user: str, max_tokens: int = 4096, temperature: float = 0.3) -> str:
    if not cfg.llm_base_url or not cfg.llm_api_key or not cfg.llm_model:
        raise RuntimeError("LLM 未配置，请设置 llmBaseUrl / llmApiKey / llmModel")
    response = requests.post(
        cfg.llm_base_url.rstrip("/") + "/chat/completions",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg.llm_api_key}"},
        json={"model": cfg.llm_model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "max_tokens": max_tokens, "temperature": temperature},
        timeout=120,
    )
    if not response.ok:
        raise RuntimeError(f"LLM request failed: {response.status_code} {response.text}")
    content = ((response.json().get("choices") or [{}])[0].get("message") or {}).get("content")
    if not content:
        raise RuntimeError("LLM response content is empty")
    return str(content).strip()


def _compact_to_rows(symbol: str, data: Any, period: str) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    timestamps = data.get("timestamp") or []
    rows: list[dict[str, Any]] = []
    tz = timezone(timedelta(hours=8))
    for idx, ts in enumerate(timestamps):
        dt = datetime.fromtimestamp(float(ts) / 1000, tz=tz)
        def col(name: str, default: Any = 0) -> Any:
            values = data.get(name) or []
            return values[idx] if idx < len(values) else default
        row = {
            "symbol": symbol,
            "trade_date": dt.strftime("%Y-%m-%d"),
            "timestamp": int(ts),
            "open": float(col("open") or 0),
            "high": float(col("high") or 0),
            "low": float(col("low") or 0),
            "close": float(col("close") or 0),
            "volume": float(col("volume") or 0),
            "amount": float(col("amount") or 0),
            "prev_close": safe_float(col("prev_close", None)),
        }
        if period != "1d":
            row.update({"period": period, "trade_time": dt.strftime("%H:%M:%S"), "open_interest": safe_float(col("open_interest", None)), "settlement_price": safe_float(col("settlement_price", None))})
        rows.append(row)
    return rows


def _parse_json_rpc(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("data:"):
        lines = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        text = lines[-1] if lines else text
    return json.loads(text)


def _normalize_documents(value: Any) -> list[dict[str, Any]]:
    candidates = _collect_docs(value)
    docs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        title = first_nonempty(item.get("title"), item.get("name"), item.get("headline"), "未命名资讯")
        text = first_nonempty(item.get("trunk"), item.get("content"), item.get("summary"), item.get("abstract"), item.get("text"))
        key = f"{title}\n{text}"
        if key in seen or not (title or text):
            continue
        seen.add(key)
        docs.append({"title": title, "trunk": text, "source": first_nonempty(item.get("source"), item.get("media"), item.get("sourceName")), "publishedAt": first_nonempty(item.get("publishTime"), item.get("publishedAt"), item.get("showTime"), item.get("date")), "secuList": item.get("secuList") or []})
    return docs


def _collect_docs(value: Any, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 5:
        return []
    if isinstance(value, list):
        out: list[dict[str, Any]] = []
        for item in value:
            out.extend(_collect_docs(item, depth + 1))
        return out
    if not isinstance(value, dict):
        return []
    if any(k in value for k in ["title", "headline", "trunk", "content", "summary", "abstract", "secuList"]):
        return [value]
    nested = get_nested(value, "data", "data", "llmSearchResponse", "data")
    if nested is not None:
        return _collect_docs(nested, depth + 1)
    out = []
    for item in value.values():
        out.extend(_collect_docs(item, depth + 1))
    return out
