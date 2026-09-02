from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect

from libs.db.models.message import ToolCallListType
from libs.llm.base import ToolCall

DIALECT: Dialect = postgresql.dialect()


def test_process_bind_param_and_result_value_round_trip_preserves_fields() -> None:
    tool_calls = [
        ToolCall(id="c1", name="write_document", arguments={"path": "report.docx", "n": 1}),
        ToolCall(id="c2", name="search", arguments={"query": "август", "nested": {"a": [1, 2]}}),
    ]
    type_ = ToolCallListType()

    bound = type_.process_bind_param(tool_calls, DIALECT)
    loaded = type_.process_result_value(bound, DIALECT)

    assert loaded == tool_calls
    assert [call.id for call in loaded or []] == ["c1", "c2"]
    assert [call.name for call in loaded or []] == ["write_document", "search"]
    assert [call.arguments for call in loaded or []] == [
        {"path": "report.docx", "n": 1},
        {"query": "август", "nested": {"a": [1, 2]}},
    ]


def test_process_bind_param_and_result_value_handle_empty_list() -> None:
    type_ = ToolCallListType()

    bound = type_.process_bind_param([], DIALECT)
    loaded = type_.process_result_value(bound, DIALECT)

    assert bound == []
    assert loaded == []


def test_process_bind_param_and_result_value_handle_none() -> None:
    type_ = ToolCallListType()

    assert type_.process_bind_param(None, DIALECT) is None
    assert type_.process_result_value(None, DIALECT) is None


def test_bind_and_result_processor_round_trip_through_jsonb_pipeline() -> None:
    tool_calls = [ToolCall(id="c1", name="write_document", arguments={"path": "report.docx"})]
    type_ = ToolCallListType()

    bind = type_.bind_processor(DIALECT)
    result = type_.result_processor(DIALECT, None)
    assert bind is not None
    assert result is not None

    serialized = bind(tool_calls)
    assert isinstance(serialized, str)
    assert result(serialized) == tool_calls


def test_bind_processor_encodes_none_as_sql_null_not_json_null() -> None:
    """none_as_null=True делает bind-параметр Python None (SQL NULL), а не строку 'null'.

    Без него JSONB сериализовал бы None в JSON-литерал 'null' внутри значения колонки —
    round-trip через process_result_value этого не ловит, потому что JSON null тоже
    десериализуется обратно в Python None.
    """
    type_ = ToolCallListType()

    bind = type_.bind_processor(DIALECT)
    assert bind is not None

    assert bind(None) is None
