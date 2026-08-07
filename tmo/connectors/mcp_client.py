"""Минимальный клиент MCP поверх HTTP.

Сервер Туту отвечает на JSON-RPC без установки сессии, но заголовок
``Accept`` обязан допускать и ``text/event-stream``: часть развёртываний
отдаёт ответ потоком событий, и клиент, объявивший только JSON, получает 406.

Схемы инструментов читаются во время выполнения. Это не перестраховка: имена
аргументов менялись между версиями сервера, а жёстко зашитое имя перестаёт
работать **без всякой ошибки** — параметр просто игнорируется, и выдача
приходит по другому запросу (SOURCES_PLAYBOOK §1).
"""

from __future__ import annotations

import json
from typing import Any

from tmo.connectors.transport import SourceTransport, TimeBudget
from tmo.core.errors import ConnectorSchemaError
from tmo.core.logging import get_logger

logger = get_logger(__name__)

_ACCEPT = "application/json, text/event-stream"


def _parse_sse(text: str) -> Any:
    """Достаёт полезную нагрузку из потока событий."""
    for line in text.splitlines():
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk and chunk != "[DONE]":
                try:
                    return json.loads(chunk)
                except json.JSONDecodeError:
                    continue
    raise ConnectorSchemaError("Поток событий MCP не содержит разбираемых данных")


class McpClient:
    """JSON-RPC поверх ``SourceTransport``: allowlist и лимит темпа общие."""

    def __init__(self, transport: SourceTransport, endpoint: str, *, client_name: str) -> None:
        self.transport = transport
        self.endpoint = endpoint
        self.client_name = client_name
        self._request_id = 0
        self._tools: dict[str, dict[str, Any]] | None = None
        self.server_version: str | None = None

    # -- низкий уровень ------------------------------------------------------

    def _call(self, method: str, params: dict[str, Any] | None = None,
              *, budget: TimeBudget | None = None) -> Any:
        self._request_id += 1
        body = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params or {},
        }
        response = self.transport.post_json(
            self.endpoint, body, headers={"Accept": _ACCEPT}, budget=budget
        )
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            payload = _parse_sse(response.text)
        else:
            try:
                payload = response.json()
            except ValueError as exc:
                raise ConnectorSchemaError(
                    f"MCP вернул неразбираемое тело ({content_type}): {response.text[:200]}",
                    source_code=self.transport.source_code,
                ) from exc

        if not isinstance(payload, dict):
            raise ConnectorSchemaError(
                f"Ожидался объект JSON-RPC, получено {type(payload).__name__}",
                source_code=self.transport.source_code,
            )
        if payload.get("error"):
            error = payload["error"]
            raise ConnectorSchemaError(
                f"MCP {method} вернул ошибку {error.get('code')}: {error.get('message')}",
                source_code=self.transport.source_code,
            )
        return payload.get("result")

    # -- высокий уровень -----------------------------------------------------

    def initialize(self, *, budget: TimeBudget | None = None) -> dict[str, Any]:
        result = self._call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": self.client_name, "version": "2.0.0"},
            },
            budget=budget,
        )
        info = (result or {}).get("serverInfo") or {}
        self.server_version = f"{info.get('name', 'mcp')}/{info.get('version', '?')}"
        return result or {}

    def tool_schemas(self, *, budget: TimeBudget | None = None) -> dict[str, dict[str, Any]]:
        if self._tools is not None:
            return self._tools
        result = self._call("tools/list", budget=budget) or {}
        tools: dict[str, dict[str, Any]] = {}
        for tool in result.get("tools") or []:
            name = tool.get("name")
            if not name:
                continue
            schema = tool.get("inputSchema") or tool.get("input_schema") or {}
            tools[str(name)] = schema if isinstance(schema, dict) else {}
        if not tools:
            raise ConnectorSchemaError(
                "MCP-сервер не вернул ни одного инструмента",
                source_code=self.transport.source_code,
            )
        self._tools = tools
        return tools

    def call_tool(
        self, name: str, arguments: dict[str, Any], *, budget: TimeBudget | None = None
    ) -> Any:
        result = self._call("tools/call", {"name": name, "arguments": arguments}, budget=budget)
        if not isinstance(result, dict):
            raise ConnectorSchemaError(
                f"Инструмент {name} вернул {type(result).__name__} вместо объекта",
                source_code=self.transport.source_code,
            )
        if result.get("isError"):
            text = _first_text(result) or json.dumps(result, ensure_ascii=False)[:300]
            raise ConnectorSchemaError(
                f"Инструмент {name} сообщил об ошибке: {text[:300]}",
                source_code=self.transport.source_code,
            )
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        text = _first_text(result)
        if text is None:
            raise ConnectorSchemaError(
                f"Инструмент {name} вернул пустой ответ",
                source_code=self.transport.source_code,
            )
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConnectorSchemaError(
                f"Инструмент {name} вернул неразбираемый текст: {text[:200]}",
                source_code=self.transport.source_code,
            ) from exc


def _first_text(result: dict[str, Any]) -> str | None:
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            return str(item.get("text") or "")
    return None


# --------------------------------------------------------------------------- #
# Работа со схемой инструмента
# --------------------------------------------------------------------------- #


def property_type(schema: dict[str, Any], name: str) -> str:
    """Объявленный тип аргумента с учётом ``anyOf``/``oneOf`` и ``null``."""
    prop = (schema.get("properties") or {}).get(name) or {}
    declared = prop.get("type")
    if isinstance(declared, list):
        for item in declared:
            if item != "null":
                return str(item)
    if declared:
        return str(declared)
    for key in ("anyOf", "oneOf"):
        for variant in prop.get(key) or []:
            if isinstance(variant, dict) and variant.get("type") not in (None, "null"):
                return str(variant["type"])
    return "string"


def item_type(schema: dict[str, Any], name: str) -> str:
    prop = (schema.get("properties") or {}).get(name) or {}
    for variant in (prop, *(prop.get("anyOf") or []), *(prop.get("oneOf") or [])):
        if not isinstance(variant, dict) or variant.get("type") != "array":
            continue
        items = variant.get("items")
        if isinstance(items, dict) and items.get("type"):
            return str(items["type"])
    return "string"


def bounds(schema: dict[str, Any], name: str) -> tuple[float | None, float | None]:
    """Границы, объявленные схемой.

    Читаешь схему — соблюдай и её ограничения, а не только имена: ``page_size``
    сверх объявленного максимума отклоняет запрос целиком, и наблюдение
    выглядит пустым рынком.
    """
    prop = (schema.get("properties") or {}).get(name) or {}
    variants = [prop, *(prop.get("anyOf") or []), *(prop.get("oneOf") or [])]
    minimum = maximum = None
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        if variant.get("minimum") is not None and minimum is None:
            minimum = float(variant["minimum"])
        if variant.get("maximum") is not None and maximum is None:
            maximum = float(variant["maximum"])
    return minimum, maximum


class ArgumentBuilder:
    """Собирает аргументы вызова строго по фактической схеме инструмента."""

    def __init__(self, tool: str, schema: dict[str, Any], *, source_code: str) -> None:
        self.tool = tool
        self.schema = schema
        self.source_code = source_code
        self.properties: dict[str, Any] = schema.get("properties") or {}
        self.args: dict[str, Any] = {}
        self.adjustments: list[str] = []

    def has(self, *candidates: str) -> str | None:
        for candidate in candidates:
            if candidate in self.properties:
                return candidate
        return None

    def set(self, value: Any, *candidates: str, required: bool = False) -> str | None:
        """Кладёт значение под первым именем, которое схема действительно знает."""
        if value is None:
            return None
        name = self.has(*candidates)
        if name is None:
            if required:
                raise ConnectorSchemaError(
                    f"Схема {self.tool} не содержит ни одного из аргументов {candidates}; "
                    f"доступны {sorted(self.properties)}",
                    source_code=self.source_code,
                )
            return None

        target = property_type(self.schema, name)
        coerced = self._coerce(value, target, name)
        minimum, maximum = bounds(self.schema, name)
        if isinstance(coerced, (int, float)) and not isinstance(coerced, bool):
            if maximum is not None and coerced > maximum:
                self.adjustments.append(f"{name}: {coerced} → {maximum} (максимум схемы)")
                coerced = type(coerced)(maximum)
            elif minimum is not None and coerced < minimum:
                self.adjustments.append(f"{name}: {coerced} → {minimum} (минимум схемы)")
                coerced = type(coerced)(minimum)
        self.args[name] = coerced
        return name

    def _coerce(self, value: Any, target: str, name: str) -> Any:
        if target == "array":
            items = value if isinstance(value, (list, tuple)) else [value]
            element = item_type(self.schema, name)
            return [self._coerce(item, element, name) for item in items]
        if target == "integer":
            try:
                return int(value)
            except (TypeError, ValueError):
                return value
        if target == "number":
            try:
                return float(value)
            except (TypeError, ValueError):
                return value
        if target == "boolean":
            return bool(value)
        if target == "string":
            return value if isinstance(value, str) else _stringify(value)
        return value


def _stringify(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
