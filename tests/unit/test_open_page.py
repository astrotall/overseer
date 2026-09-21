from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import pytest

from libs.tools import OpenPageTool
from libs.tools.open_page import (
    MAX_CONTENT_CHARS,
    MAX_TITLE_CHARS,
    MAX_URL_IN_ERROR_CHARS,
    MAX_URL_LENGTH,
    NAVIGATION_TIMEOUT_MS,
    REFRESH_TIMEOUT_MS,
    Refresh,
    _is_file_download,
    build_page_summary,
    page_timeout_text,
    parse_refresh,
    refresh_target,
    refresh_timeout_text,
)


@dataclass
class FakeResponse:
    status: int
    headers: dict[str, str]


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


@pytest.mark.parametrize(
    ("status", "headers", "expected"),
    [
        (200, {"content-type": "application/pdf"}, True),
        (200, {"content-type": "application/zip"}, True),
        (
            200,
            {"content-type": "text/html", "content-disposition": "attachment; filename=a.html"},
            True,
        ),
        (200, {"content-type": "text/html", "content-disposition": "  Attachment"}, True),
        (200, {"content-type": "text/html; charset=utf-8"}, False),
        (200, {"content-type": "text/html", "content-disposition": "inline"}, False),
        (200, {}, False),
        (302, {"content-type": "application/json"}, False),
        (404, {"content-type": "application/pdf"}, False),
    ],
)
def test_only_a_successful_non_html_or_attached_response_counts_as_a_file_download(
    status: int, headers: dict[str, str], expected: bool
) -> None:
    response = cast(Any, FakeResponse(status, headers))

    assert _is_file_download(response) is expected


def test_a_timeout_error_names_the_url_and_the_wait() -> None:
    text = page_timeout_text("https://slow.example/report")

    assert "https://slow.example/report" in text
    assert f"{NAVIGATION_TIMEOUT_MS // 1000} с" in text


def test_a_very_long_url_is_shortened_in_a_timeout_error() -> None:
    text = page_timeout_text("https://slow.example/" + "a" * 5000)

    assert len(text) < MAX_URL_IN_ERROR_CHARS + 200
    assert "…" in text


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("0; url=/next", Refresh(0, "/next")),
        ("0;URL='/next'", Refresh(0, "/next")),
        ('5 ; url = "https://example.com/a b" trailing', Refresh(5, "https://example.com/a b")),
        ("0, /next", Refresh(0, "/next")),
        ("0; /next", Refresh(0, "/next")),
        ("1.9; url=/next", Refresh(1, "/next")),
        (".5; url=/next", Refresh(0, "/next")),
        ("  3  ", Refresh(3, None)),
        ("3", Refresh(3, None)),
        ("0; url=", Refresh(0, None)),
        ("0; urn:x", Refresh(0, "urn:x")),
        ("0; url /next", Refresh(0, "url /next")),
    ],
)
def test_a_refresh_is_parsed_like_the_browser_parses_it(content: str, expected: Refresh) -> None:
    assert parse_refresh(content) == expected


@pytest.mark.parametrize("content", ["", "soon", "url=/next", "0x; url=/next", "-1; url=/next"])
def test_a_malformed_refresh_is_not_a_refresh(content: str) -> None:
    assert parse_refresh(content) is None


@pytest.mark.parametrize(
    ("refresh", "base", "expected"),
    [
        (Refresh(0, "/next"), "https://example.com/stub", "https://example.com/next"),
        (
            Refresh(0, "next?x=1"),
            "https://example.com/dir/stub",
            "https://example.com/dir/next?x=1",
        ),
        (Refresh(0, "https://other.example/"), "https://example.com/", "https://other.example/"),
        (Refresh(3, None), "https://example.com/stub", None),
        (Refresh(0, "/stub"), "https://example.com/stub", None),
        (Refresh(0, "#top"), "https://example.com/stub", None),
        (Refresh(0, "HTTPS://Example.COM:443"), "https://example.com/", None),
        (Refresh(0, "/stub?page=2"), "https://example.com/stub", "https://example.com/stub?page=2"),
        (None, "https://example.com/stub", None),
    ],
)
def test_only_a_refresh_to_another_document_has_a_target(
    refresh: Refresh | None, base: str, expected: str | None
) -> None:
    assert refresh_target(refresh, base) == expected


def test_a_refresh_timeout_names_the_target_and_the_wait() -> None:
    text = refresh_timeout_text("https://slow.example/target")

    assert "https://slow.example/target" in text
    assert f"{REFRESH_TIMEOUT_MS // 1000} с" in text


def test_a_pending_refresh_is_reported_only_when_there_is_one() -> None:
    plain = build_page_summary(url="https://example.com/", title="t", paragraphs=[], fallback="")
    pending = build_page_summary(
        url="https://example.com/",
        title="t",
        paragraphs=[],
        fallback="",
        refresh_url="https://example.com/next",
    )

    assert "refresh_url" not in plain.model_dump(exclude_none=True)
    assert pending.model_dump(exclude_none=True)["refresh_url"] == "https://example.com/next"
