from __future__ import annotations

import uuid

import httpx
import pytest

from apps.voice.confirmations import (
    ConfirmationError,
    ConfirmationGoneError,
    Decision,
    HTTPConfirmationAPI,
    classify,
)

CONFIRMATION_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def api(handler: object) -> HTTPConfirmationAPI:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return HTTPConfirmationAPI("http://localhost:8000", transport=transport)


class TestClassify:
    @pytest.mark.parametrize(
        "text",
        [
            "да",
            "Да.",
            "да, конечно",
            "ага",
            "подтверждаю",
            "ок",
            "хорошо, давай",
            "Согласен!",
        ],
    )
    def test_an_agreement_is_a_confirmation(self, text: str) -> None:
        assert classify(text) is Decision.CONFIRM

    @pytest.mark.parametrize(
        "text",
        [
            "нет",
            "Нет!",
            "не надо",
            "не нужно",
            "отмена",
            "отклони",
            "стоп",
            "нет, отмени",
        ],
    )
    def test_a_refusal_is_a_rejection(self, text: str) -> None:
        assert classify(text) is Decision.REJECT

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "...",
            "а что там было",
            "напиши письмо коллеге",
            "да нет наверное",
            "да не надо",
        ],
    )
    def test_anything_else_is_unclear(self, text: str) -> None:
        assert classify(text) is Decision.UNCLEAR

    def test_a_keyword_glued_to_a_longer_word_is_not_a_decision(self) -> None:
        assert classify("даже не думай смотреть") is Decision.REJECT
        assert classify("даже так") is Decision.UNCLEAR


class TestResolve:
    async def test_a_confirmation_goes_to_the_confirm_endpoint(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"role": "assistant", "content": "Файл удалён."})

        resolution = await api(handler).resolve(CONFIRMATION_ID, approved=True)

        assert resolution.reply == "Файл удалён."
        assert resolution.pending is None
        assert seen[0].method == "POST"
        assert seen[0].url.path == f"/confirmations/{CONFIRMATION_ID}/confirm"

    async def test_a_rejection_goes_to_the_reject_endpoint(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"role": "assistant", "content": "Отменил."})

        resolution = await api(handler).resolve(CONFIRMATION_ID, approved=False)

        assert resolution.reply == "Отменил."
        assert seen[0].url.path == f"/confirmations/{CONFIRMATION_ID}/reject"

    async def test_another_pause_comes_back_as_the_next_confirmation(self) -> None:
        following = uuid.uuid4()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                202,
                json={"confirmation_id": str(following), "summary": "Отправить письмо?"},
            )

        resolution = await api(handler).resolve(CONFIRMATION_ID, approved=True)

        assert resolution.reply is None
        assert resolution.pending is not None
        assert resolution.pending.confirmation_id == following
        assert resolution.pending.summary == "Отправить письмо?"

    @pytest.mark.parametrize("status", [404, 409])
    async def test_an_expired_or_busy_confirmation_is_told_apart(self, status: int) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"detail": "нет такого"})

        with pytest.raises(ConfirmationGoneError):
            await api(handler).resolve(CONFIRMATION_ID, approved=True)

    async def test_a_network_failure_becomes_a_confirmation_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("api недоступен", request=request)

        with pytest.raises(ConfirmationError) as caught:
            await api(handler).resolve(CONFIRMATION_ID, approved=True)

        assert not isinstance(caught.value, ConfirmationGoneError)

    async def test_a_server_failure_is_not_mistaken_for_a_resolved_confirmation(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        with pytest.raises(ConfirmationError):
            await api(handler).resolve(CONFIRMATION_ID, approved=True)

    async def test_a_reply_of_the_wrong_shape_is_an_error_not_a_silent_none(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"role": "не роль"})

        with pytest.raises(ConfirmationError):
            await api(handler).resolve(CONFIRMATION_ID, approved=True)

    async def test_the_client_closes_its_connections(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"role": "assistant", "content": "готово"})

        client = api(handler)
        await client.resolve(CONFIRMATION_ID, approved=True)
        await client.aclose()
