from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from libs.core.exceptions import ConfigurationError, NotFoundError
from libs.llm import ToolSpec
from libs.tools import EchoTool, Tool, ToolRegistry, ToolResult


class OtherArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: int


class OtherEchoTool(Tool[OtherArguments]):
    name = "echo"
    description = "Другой инструмент, случайно объявивший то же имя"
    arguments_model = OtherArguments

    async def _execute(self, arguments: OtherArguments) -> ToolResult:
        return ToolResult.ok(summary=str(arguments.value))


def test_a_registered_tool_can_be_looked_up_by_name() -> None:
    registry = ToolRegistry()
    tool = EchoTool()

    registry.register(tool)

    assert registry.get("echo") is tool


def test_registering_a_second_tool_under_the_same_name_is_a_loud_error() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())

    with pytest.raises(ConfigurationError):
        registry.register(OtherEchoTool())

    assert registry.get("echo") is not None


def test_looking_up_an_unregistered_name_is_a_loud_error() -> None:
    registry = ToolRegistry()

    with pytest.raises(NotFoundError):
        registry.get("nonexistent")


def test_list_specs_builds_the_spec_of_every_registered_tool() -> None:
    registry = ToolRegistry()
    tool = EchoTool()
    registry.register(tool)

    specs = registry.list_specs()

    assert specs == [tool.to_spec()]
    assert specs == [ToolSpec.from_model(tool.name, tool.description, tool.arguments_model)]


def test_list_specs_is_empty_for_a_fresh_registry() -> None:
    registry = ToolRegistry()

    assert registry.list_specs() == []
