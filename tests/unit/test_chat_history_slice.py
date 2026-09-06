from __future__ import annotations

from apps.api.services import cut_to_turn_boundary
from libs.llm.base import ChatMessage, ToolCall

CALL = ToolCall(id="call-1", name="echo", arguments={"text": "привет"})

USER = ChatMessage(role="user", content="вопрос")
REQUEST = ChatMessage(role="assistant", content="сейчас", tool_calls=[CALL])
RESULT = ChatMessage(role="tool", content='{"status": "ok"}', tool_call_id=CALL.id)
ANSWER = ChatMessage(role="assistant", content="ответ")


def test_a_slice_that_already_starts_on_a_user_message_is_left_alone() -> None:
    history = [USER, REQUEST, RESULT, ANSWER]

    assert cut_to_turn_boundary(history) == history


def test_an_orphan_tool_result_at_the_front_is_cut_away_with_the_rest_of_its_turn() -> None:
    history = [RESULT, ANSWER, USER, ANSWER]

    assert cut_to_turn_boundary(history) == [USER, ANSWER]


def test_an_assistant_message_at_the_front_is_cut_away_too() -> None:
    history = [REQUEST, RESULT, ANSWER, USER, ANSWER]

    assert cut_to_turn_boundary(history) == [USER, ANSWER]


def test_cutting_may_keep_fewer_messages_than_the_slice_held() -> None:
    history = [REQUEST, RESULT, ANSWER, USER]

    kept = cut_to_turn_boundary(history)

    assert kept == [USER]
    assert len(kept) < len(history)


def test_a_slice_without_a_single_user_message_keeps_nothing() -> None:
    assert cut_to_turn_boundary([REQUEST, RESULT, ANSWER]) == []


def test_an_empty_slice_stays_empty() -> None:
    assert cut_to_turn_boundary([]) == []
