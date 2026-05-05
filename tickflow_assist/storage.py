from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterable


SCHEMAS: dict[str, list[tuple[str, str, bool]]] = {
    "watchlist": [("symbol", "str", False), ("name", "str", True), ("costPrice", "float", False), ("addedAt", "str", False), ("sector", "str", True), ("themes", "str", True), ("themeQuery", "str", True), ("themeUpdatedAt", "str", True)],
    "universes": [("id", "str", False), ("name", "str", False), ("description", "str", True), ("region", "str", False), ("category", "str", False), ("symbolCount", "int", False), ("syncedAt", "str", False)],
    "universe_memberships": [("universeId", "str", False), ("symbol", "str", False)],
    "klines_daily": [("symbol", "str", False), ("trade_date", "str", False), ("timestamp", "int", False), ("open", "float", False), ("high", "float", False), ("low", "float", False), ("close", "float", False), ("volume", "float", False), ("amount", "float", False), ("prev_close", "float", True)],
    "klines_intraday": [("symbol", "str", False), ("period", "str", False), ("trade_date", "str", False), ("trade_time", "str", False), ("timestamp", "int", False), ("open", "float", False), ("high", "float", False), ("low", "float", False), ("close", "float", False), ("volume", "float", False), ("amount", "float", False), ("prev_close", "float", True), ("open_interest", "float", True), ("settlement_price", "float", True)],
    "indicators": [("symbol", "str", False), ("trade_date", "str", False), ("ma5", "float", True), ("ma10", "float", True), ("ma20", "float", True), ("ma60", "float", True), ("macd", "float", True), ("macd_signal", "float", True), ("macd_hist", "float", True), ("kdj_k", "float", True), ("kdj_d", "float", True), ("kdj_j", "float", True), ("rsi_6", "float", True), ("rsi_12", "float", True), ("rsi_24", "float", True), ("cci", "float", True), ("bias_6", "float", True), ("bias_12", "float", True), ("bias_24", "float", True), ("plus_di", "float", True), ("minus_di", "float", True), ("adx", "float", True), ("boll_upper", "float", True), ("boll_mid", "float", True), ("boll_lower", "float", True)],
    "key_levels": [("symbol", "str", False), ("analysis_date", "str", False), ("current_price", "float", False), ("stop_loss", "float", True), ("breakthrough", "float", True), ("support", "float", True), ("cost_level", "float", True), ("resistance", "float", True), ("take_profit", "float", True), ("gap", "float", True), ("target", "float", True), ("round_number", "float", True), ("analysis_text", "str", False), ("score", "int", False)],
    "key_levels_history": [("symbol", "str", False), ("analysis_date", "str", False), ("activated_at", "str", False), ("profile", "str", False), ("current_price", "float", False), ("stop_loss", "float", True), ("breakthrough", "float", True), ("support", "float", True), ("cost_level", "float", True), ("resistance", "float", True), ("take_profit", "float", True), ("gap", "float", True), ("target", "float", True), ("round_number", "float", True), ("analysis_text", "str", False), ("score", "int", True)],
    "analysis_log": [("symbol", "str", False), ("analysis_date", "str", False), ("analysis_text", "str", False), ("structured_ok", "int", False)],
    "technical_analysis": [("symbol", "str", False), ("analysis_date", "str", False), ("analysis_text", "str", False), ("structured_ok", "int", False), ("current_price", "float", True), ("stop_loss", "float", True), ("breakthrough", "float", True), ("support", "float", True), ("cost_level", "float", True), ("resistance", "float", True), ("take_profit", "float", True), ("gap", "float", True), ("target", "float", True), ("round_number", "float", True), ("score", "int", True)],
    "financial_analysis": [("symbol", "str", False), ("analysis_date", "str", False), ("analysis_text", "str", False), ("score", "int", True), ("bias", "str", False), ("strengths_json", "str", False), ("risks_json", "str", False), ("watch_items_json", "str", False), ("evidence_json", "str", False)],
    "news_analysis": [("symbol", "str", False), ("analysis_date", "str", False), ("query", "str", False), ("analysis_text", "str", False), ("score", "int", True), ("bias", "str", False), ("catalysts_json", "str", False), ("risks_json", "str", False), ("watch_items_json", "str", False), ("source_count", "int", False), ("evidence_json", "str", False)],
    "composite_analysis": [("symbol", "str", False), ("analysis_date", "str", False), ("analysis_text", "str", False), ("structured_ok", "int", False), ("current_price", "float", True), ("stop_loss", "float", True), ("breakthrough", "float", True), ("support", "float", True), ("cost_level", "float", True), ("resistance", "float", True), ("take_profit", "float", True), ("gap", "float", True), ("target", "float", True), ("round_number", "float", True), ("score", "int", True), ("technical_score", "int", True), ("financial_score", "int", True), ("news_score", "int", True), ("financial_bias", "str", False), ("news_bias", "str", False), ("evidence_json", "str", False)],
    "alert_log": [("symbol", "str", False), ("alert_date", "str", False), ("rule_name", "str", False), ("message", "str", False), ("triggered_at", "str", False)],
    "jin10_flash": [("flash_key", "str", False), ("published_at", "str", False), ("published_ts", "int", False), ("content", "str", False), ("url", "str", False), ("ingested_at", "str", False), ("raw_json", "str", False)],
    "jin10_flash_delivery": [("flash_key", "str", False), ("published_at", "str", False), ("symbols_json", "str", False), ("headline", "str", False), ("reason", "str", False), ("importance", "str", False), ("message", "str", False), ("delivered_at", "str", False)],
}


class LanceStore:
    def __init__(self, path: str):
        self.path = Path(path).expanduser()
        self.path.mkdir(parents=True, exist_ok=True)
        self._db = None

    @property
    def db(self):
        if self._db is None:
            try:
                import lancedb
            except ImportError as exc:
                raise RuntimeError(
                    f"无法导入 Python 依赖 lancedb（当前 Python: {sys.executable}）。"
                    f"请使用最新 ./setup-tickflow.sh 重新安装并重启 Hermes；原始错误: {exc}"
                ) from exc
            self._db = lancedb.connect(str(self.path))
        return self._db

    def table_names(self) -> list[str]:
        names = getattr(self.db, "table_names", None)
        if callable(names):
            return list(names())
        names = getattr(self.db, "list_tables", None)
        return list(names()) if callable(names) else []

    def has_table(self, name: str) -> bool:
        if name in self.table_names():
            return True
        try:
            self.open(name)
            return True
        except Exception:
            return False

    def open(self, name: str):
        return self.db.open_table(name)

    def ensure(self, name: str, rows: list[dict[str, Any]] | None = None):
        if self.has_table(name):
            return self.open(name)
        rows = rows or [_empty_row(name)]
        try:
            table = self.db.create_table(name, data=_coerce_rows(name, rows), schema=_arrow_schema(name))
        except Exception as exc:
            if _table_already_exists(exc):
                return self.open(name)
            raise
        if rows and rows == [_empty_row(name)]:
            table.delete(_all_rows_predicate())
        return table

    def add(self, name: str, rows: Iterable[dict[str, Any]]) -> None:
        normalized = _coerce_rows(name, list(rows))
        if not normalized:
            return
        table = self.ensure(name, normalized)
        table.add(normalized)

    def replace_where(self, name: str, predicate: str, rows: Iterable[dict[str, Any]]) -> None:
        row_list = list(rows)
        normalized = _coerce_rows(name, row_list)
        if not self.has_table(name):
            if normalized:
                try:
                    self.db.create_table(name, data=normalized, schema=_arrow_schema(name))
                    return
                except Exception as exc:
                    if not _table_already_exists(exc):
                        raise
            else:
                self.ensure(name)
                return
        table = self.open(name)
        try:
            table.delete(predicate)
        except Exception:
            pass
        if normalized:
            table.add(normalized)

    def rows(self, name: str) -> list[dict[str, Any]]:
        try:
            table = self.open(name)
        except Exception:
            return []
        if hasattr(table, "to_pandas"):
            return _records_from_frame(table.to_pandas())
        if hasattr(table, "search"):
            return [dict(row) for row in table.search().limit(1_000_000).to_list()]
        return []

    def schema_description(self, name: str) -> list[dict[str, Any]]:
        if name in SCHEMAS:
            return [{"name": n, "type": t, "nullable": nullable} for n, t, nullable in SCHEMAS[name]]
        try:
            schema = self.open(name).schema
        except Exception:
            return []
        schema = schema() if callable(schema) else schema
        return [{"name": field.name, "type": str(field.type), "nullable": field.nullable} for field in schema]


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _records_from_frame(frame) -> list[dict[str, Any]]:
    frame = frame.where(frame.notna(), None)
    return frame.to_dict(orient="records")


def _arrow_schema(name: str):
    import pyarrow as pa

    type_map = {"str": pa.utf8(), "float": pa.float64(), "int": pa.int64()}
    return pa.schema([pa.field(field, type_map[kind], nullable=nullable) for field, kind, nullable in SCHEMAS[name]])


def _empty_row(name: str) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for field, kind, nullable in SCHEMAS[name]:
        if nullable:
            row[field] = None
        elif kind == "str":
            row[field] = ""
        elif kind == "int":
            row[field] = 0
        else:
            row[field] = 0.0
    return row


def _coerce_rows(name: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    schema = SCHEMAS.get(name)
    if not schema:
        return rows
    output: list[dict[str, Any]] = []
    for row in rows:
        item = _empty_row(name)
        for field, kind, nullable in schema:
            value = row.get(field)
            if value is None and nullable:
                item[field] = None
            elif kind == "str":
                item[field] = "" if value is None else str(value)
            elif kind == "int":
                item[field] = 0 if value is None else int(value)
            else:
                item[field] = 0.0 if value is None else float(value)
        output.append(item)
    return output


def _all_rows_predicate() -> str:
    return "1 = 1"


def _table_already_exists(exc: Exception) -> bool:
    text = str(exc).lower()
    return "already exists" in text and "table" in text
