from __future__ import annotations

from typing import Any

from libs.core.exceptions import ConfigurationError, NotFoundError
from libs.llm.base import ToolSpec
from libs.tools.base import Tool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool[Any]] = {}

    def register(self, tool: Tool[Any]) -> None:
        if tool.name in self._tools:
            raise ConfigurationError(f"Инструмент с именем '{tool.name}' уже зарегистрирован")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool[Any]:
        try:
            return self._tools[name]
        except KeyError:
            raise NotFoundError(f"Инструмент '{name}' не зарегистрирован") from None

    def list_specs(self) -> list[ToolSpec]:
        return [tool.to_spec() for tool in self._tools.values()]


_registry: ToolRegistry | None = None


def init_tool_registry() -> ToolRegistry:
    global _registry

    if _registry is not None:
        return _registry

    _registry = ToolRegistry()
    return _registry


def get_tool_registry() -> ToolRegistry:
    if _registry is None:
        raise ConfigurationError("ToolRegistry не инициализирован: вызовите init_tool_registry()")
    return _registry


def reset_tool_registry() -> None:
    global _registry
    _registry = None
