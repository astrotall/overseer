from __future__ import annotations

from typing import Any

import pytest

from libs.tools import SearchResult, WebSearchTool
from libs.tools.web_search import (
    MAX_QUERY_LENGTH,
    MAX_RESULTS,
    MAX_SNIPPET_CHARS,
    parse_results,
    resolve_result_url,
)


def _raw(
    title: str = "Python.org",
    href: str = "https://www.python.org/",
    snippet: str = "Официальный сайт",
    *,
    ad: bool = False,
) -> dict[str, Any]:
    return {"title": title, "href": href, "snippet": snippet, "ad": ad}


def test_the_model_sees_only_the_query_in_the_schema() -> None:
    schema = WebSearchTool().to_spec().input_schema

    assert list(schema["properties"]) == ["query"]
    assert schema["required"] == ["query"]
    assert "conversation_id" not in schema["properties"]


def test_searching_does_not_ask_for_confirmation() -> None:
    assert WebSearchTool.requires_confirmation is False


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("missing_query", {}),
        ("blank_query", {"query": "   "}),
        ("too_long_query", {"query": "я" * (MAX_QUERY_LENGTH + 1)}),
        ("unknown_field", {"query": "погода", "max_results": 50}),
    ],
)
def test_arguments_the_model_got_wrong_are_rejected(name: str, arguments: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="validation error"):
        WebSearchTool.arguments_model.model_validate(arguments)


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("https://www.python.org/", "https://www.python.org/"),
        (
            "//duckduckgo.com/l/?uddg=https%3A%2F%2Fru.wikipedia.org%2Fwiki%2FPython&rut=abc",
            "https://ru.wikipedia.org/wiki/Python",
        ),
        (
            "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F%3Fq%3D1",
            "https://docs.python.org/3/?q=1",
        ),
        ("https://duckduckgo.com/y.js?ad_domain=shop.example&u3=x", None),
        ("//duckduckgo.com/l/?uddg=https%3A%2F%2Fduckduckgo.com%2Fl%2F", None),
        ("/html/?q=next", None),
        ("javascript:void(0)", None),
        ("", None),
    ],
)
def test_links_are_resolved_to_the_page_itself(href: str, expected: str | None) -> None:
    assert resolve_result_url(href) == expected


def test_results_are_cleaned_and_come_back_in_page_order() -> None:
    results = parse_results(
        [
            _raw("  Welcome to\n  Python.org ", snippet="The official\n\n home of <Python>"),
            _raw("Python — Википедия", "https://ru.wikipedia.org/wiki/Python", ""),
        ]
    )

    assert results == [
        SearchResult(
            title="Welcome to Python.org",
            url="https://www.python.org/",
            snippet="The official home of <Python>",
        ),
        SearchResult(
            title="Python — Википедия", url="https://ru.wikipedia.org/wiki/Python", snippet=""
        ),
    ]


def test_ads_untitled_entries_and_duplicate_links_are_dropped() -> None:
    results = parse_results(
        [
            _raw("Купить Python со скидкой", "https://shop.example/", ad=True),
            _raw("", "https://empty.example/"),
            _raw("Без ссылки", "/html/?q=next"),
            _raw("Python.org"),
            _raw("Python.org ещё раз"),
        ]
    )

    assert [result.url for result in results] == ["https://www.python.org/"]


def test_the_number_of_results_is_capped() -> None:
    raw = [_raw(f"Результат {index}", f"https://site{index}.example/") for index in range(30)]

    results = parse_results(raw)

    assert len(results) == MAX_RESULTS
    assert results[-1].title == f"Результат {MAX_RESULTS - 1}"


def test_a_long_snippet_does_not_grow_the_payload_without_bound() -> None:
    (result,) = parse_results([_raw(snippet="слово " * 1000)])

    assert len(result.snippet) == MAX_SNIPPET_CHARS
    assert result.snippet.endswith("…")
