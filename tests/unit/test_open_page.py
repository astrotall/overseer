from __future__ import annotations

from typing import Any

import pytest

from libs.tools import OpenPageTool
from libs.tools.open_page import (
    MAX_CONTENT_CHARS,
    MAX_TITLE_CHARS,
    MAX_URL_LENGTH,
    build_page_summary,
)


def test_the_model_sees_only_the_url_in_the_schema() -> None:
    schema = OpenPageTool().to_spec().input_schema

    assert list(schema["properties"]) == ["url"]
    assert schema["required"] == ["url"]
    assert "conversation_id" not in schema["properties"]


def test_opening_a_page_does_not_ask_for_confirmation() -> None:
    assert OpenPageTool.requires_confirmation is False


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("missing_url", {}),
        ("blank_url", {"url": "   "}),
        ("no_scheme", {"url": "example.com"}),
        ("javascript_scheme", {"url": "javascript:alert(1)"}),
        ("too_long_url", {"url": "https://example.com/" + "a" * MAX_URL_LENGTH}),
        ("unknown_field", {"url": "https://example.com", "wait_for": "load"}),
    ],
)
def test_arguments_the_model_got_wrong_are_rejected(name: str, arguments: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="validation error"):
        OpenPageTool.arguments_model.model_validate(arguments)


def test_a_valid_url_is_accepted_as_is() -> None:
    parsed = OpenPageTool.arguments_model.model_validate({"url": "https://example.com/path"})

    assert parsed.url == "https://example.com/path"


def test_the_first_paragraphs_become_the_content() -> None:
    summary = build_page_summary(
        url="https://example.com/",
        title="  Example  Domain  ",
        paragraphs=["  First   paragraph.  ", "Second paragraph.", "Third.", "Fourth — dropped."],
        fallback="ignored when paragraphs exist",
    )

    assert summary.title == "Example Domain"
    assert summary.content == "First paragraph.\n\nSecond paragraph.\n\nThird."


def test_pages_without_paragraphs_fall_back_to_the_container_text() -> None:
    summary = build_page_summary(
        url="https://example.com/",
        title="Untitled",
        paragraphs=[],
        fallback="  Some   raw text   without markup  ",
    )

    assert summary.content == "Some raw text without markup"


def test_a_page_without_title_or_content_is_legitimately_empty() -> None:
    summary = build_page_summary(url="https://example.com/", title="", paragraphs=[], fallback="")

    assert summary.title == ""
    assert summary.content == ""


def test_a_long_title_and_content_do_not_grow_the_payload_without_bound() -> None:
    summary = build_page_summary(
        url="https://example.com/",
        title="слово " * 100,
        paragraphs=["текст " * 1000],
        fallback="",
    )

    assert len(summary.title) == MAX_TITLE_CHARS
    assert summary.title.endswith("…")
    assert len(summary.content) == MAX_CONTENT_CHARS
    assert summary.content.endswith("…")
