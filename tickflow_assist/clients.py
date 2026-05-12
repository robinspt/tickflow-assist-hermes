from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib.parse import urljoin

import requests

from .config import Config
from .utils import as_list, first_nonempty, get_nested, normalize_symbol, safe_float


JIN10_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


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
        return self._call_tool("list_flash", {"cursor": cursor} if cursor else {})

    def _initialize(self) -> None:
        self._request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "tickflow-assist-hermes", "version": "0.3.9"}})
        self._notify("notifications/initialized")
        for method in ["tools/list", "resources/list"]:
            try:
                self._request(method, {})
            except Exception:
                pass
        self.initialized = True

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            if not self.initialized:
                self._initialize()
            result = self._request("tools/call", {"name": name, "arguments": arguments})
        except RuntimeError as exc:
            if not _is_jin10_session_not_found(exc):
                raise
            self.session_id = None
            self.initialized = False
            self._initialize()
            result = self._request("tools/call", {"name": name, "arguments": arguments})
        if not result:
            raise RuntimeError(f"jin10 tool {name} returned empty result")
        if result.get("isError"):
            raise RuntimeError(_extract_jin10_tool_error(result))
        return _extract_jin10_structured_result(result)

    def _initialize_and_call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        init_payload = {"jsonrpc": "2.0", "id": self.request_id, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "tickflow-assist-hermes", "version": "0.3.9"}}}
        self.request_id += 1
        tool_payload = {"jsonrpc": "2.0", "id": self.request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}
        self.request_id += 1
        results = self._batch_request([init_payload, tool_payload])
        return results[1] if len(results) >= 2 else (results[-1] if results else {})

    def _request(self, method: str, params: Any) -> Any:
        payload = {"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params}
        self.request_id += 1
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.token}", "X-API-Key": self.token, "Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        response = _jin10_post(self.url, headers=headers, json=payload, timeout=45)
        if response.headers.get("mcp-session-id"):
            self.session_id = response.headers["mcp-session-id"]
        response_text = _decode_response_text(response)
        if not response.ok:
            raise RuntimeError(f"jin10 MCP request failed: {response.status_code} {response_text}")
        parsed = _parse_json_rpc(response_text)
        if parsed.get("error"):
            raise RuntimeError(f"jin10 MCP error: {parsed['error']}")
        return parsed.get("result")

    def _notify(self, method: str) -> None:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.token}", "X-API-Key": self.token}
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        response = _jin10_post(self.url, headers=headers, json={"jsonrpc": "2.0", "method": method}, timeout=20)
        if response.headers.get("mcp-session-id"):
            self.session_id = response.headers["mcp-session-id"]
        if not response.ok:
            raise RuntimeError(f"jin10 MCP notification failed: {response.status_code} {_decode_response_text(response)}")

    def _batch_request(self, payloads: list[dict[str, Any]]) -> list[Any]:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.token}", "X-API-Key": self.token, "Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        response = _jin10_post(self.url, headers=headers, json=payloads, timeout=45)
        if response.headers.get("mcp-session-id"):
            self.session_id = response.headers["mcp-session-id"]
        response_text = _decode_response_text(response)
        if not response.ok:
            raise RuntimeError(f"jin10 MCP request failed: {response.status_code} {response_text}")
        parsed = _parse_json_rpc_batch(response_text)
        errors = [item.get("error") for item in parsed if isinstance(item, dict) and item.get("error")]
        if errors:
            raise RuntimeError(f"jin10 MCP error: {errors[0]}")
        return [item.get("result") for item in parsed if isinstance(item, dict)]


def _jin10_post(url: str, **kwargs) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = requests.post(url, **kwargs)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            if attempt == 1:
                raise
        else:
            if response.status_code in JIN10_TRANSIENT_STATUS_CODES and attempt == 0:
                time.sleep(1.0)
                continue
            return response
        time.sleep(1.0)
    if last_error:
        raise last_error
    raise RuntimeError("jin10 MCP request failed before response")


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
    if not text:
        return {}
    parsed = _try_json_object(text)
    if parsed is not None:
        return parsed

    for candidate in reversed(_sse_data_candidates(text)):
        parsed = _try_json_object(candidate)
        if parsed is not None:
            return parsed
    preview = text[:300].replace("\n", "\\n")
    raise RuntimeError(f"jin10 MCP response is not valid JSON/SSE JSON: {preview}")


def _parse_json_rpc_batch(raw: str) -> list[dict[str, Any]]:
    text = raw.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        return [parsed]

    results = []
    seen: set[str] = set()
    for data in _sse_data_candidates(text):
        try:
            item = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            fingerprint = json.dumps(item, sort_keys=True, ensure_ascii=False)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            results.append(item)
    if results:
        return results
    preview = text[:300].replace("\n", "\\n")
    raise RuntimeError(f"jin10 MCP batch response is not valid JSON/SSE JSON: {preview}")


def _try_json_object(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        raise RuntimeError(f"jin10 MCP JSON response is not an object: {type(parsed).__name__}")
    return parsed


def _sse_data_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for event in re.split(r"\r?\n\s*\r?\n", text):
        data_lines: list[str] = []
        for line in event.splitlines():
            stripped = line.strip()
            if not stripped.startswith("data:"):
                continue
            data = stripped[5:].strip()
            if not data or data == "[DONE]":
                continue
            data_lines.append(data)
            candidates.append(data)
        if len(data_lines) > 1:
            candidates.append("\n".join(data_lines).strip())
            candidates.append("".join(data_lines).strip())
    return [candidate for candidate in candidates if candidate and candidate != "[DONE]"]


def _decode_response_text(response: requests.Response) -> str:
    try:
        return response.content.decode("utf-8")
    except UnicodeDecodeError:
        return response.text


def _extract_jin10_tool_error(result: dict[str, Any]) -> str:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        message = structured.get("message") or structured.get("error")
        if message:
            return str(message)
    content = result.get("content")
    if isinstance(content, list):
        texts = [str(item.get("text")) for item in content if isinstance(item, dict) and item.get("text")]
        if texts:
            return "\n".join(texts)
    if isinstance(content, str) and content:
        return content
    return "Tool execution error"


def _is_jin10_session_not_found(error: Exception) -> bool:
    text = str(error).lower()
    return "session not found" in text or ("404" in text and "session" in text)


def _extract_jin10_structured_result(result: dict[str, Any]) -> Any:
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("structuredContent") is not None:
                return item["structuredContent"]
            if isinstance(item, dict) and item.get("text"):
                text = _repair_mojibake(str(item.get("text") or ""))
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    continue
        return content
    if isinstance(content, str) and content:
        text = _repair_mojibake(content)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}
    return result


def _repair_mojibake(value: str) -> str:
    try:
        repaired = value.encode("latin1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    return repaired if _mojibake_score(repaired) < _mojibake_score(value) else value


def _mojibake_score(value: str) -> int:
    return sum(value.count(ch) for ch in ("�", "Ã", "Â", "ç", "å", "è", "æ", "ï¼"))


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
