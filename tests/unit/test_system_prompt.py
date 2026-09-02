from __future__ import annotations

from libs.llm import OVERSEER_SYSTEM_PROMPT, get_system_prompt_message


def test_get_system_prompt_message_returns_system_role_with_prompt_content() -> None:
    message = get_system_prompt_message()

    assert message.role == "system"
    assert message.content == OVERSEER_SYSTEM_PROMPT
    assert message.content
