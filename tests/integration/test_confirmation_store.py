from __future__ import annotations

import asyncio
import uuid

import pytest
from redis.asyncio import Redis

from libs.confirmations import ConfirmationStore, PendingConfirmation
from libs.core.exceptions import NotFoundError


@pytest.mark.integration
async def test_create_and_get_pending_round_trips_the_stored_data(redis_client: Redis) -> None:
    store = ConfirmationStore(redis_client)
    conversation_id = uuid.uuid4()

    confirmation_id = await store.create_pending(
        conversation_id,
        "delete_file",
        {"path": "C:\\Users\\ivan\\report.docx"},
        "Удалить файл report.docx",
    )

    assert isinstance(confirmation_id, uuid.UUID)

    pending = await store.get_pending(confirmation_id)

    assert pending == PendingConfirmation(
        conversation_id=conversation_id,
        tool_name="delete_file",
        arguments={"path": "C:\\Users\\ivan\\report.docx"},
        summary="Удалить файл report.docx",
    )


@pytest.mark.integration
async def test_get_pending_with_unknown_id_raises_not_found(redis_client: Redis) -> None:
    store = ConfirmationStore(redis_client)

    with pytest.raises(NotFoundError):
        await store.get_pending(uuid.uuid4())


@pytest.mark.integration
async def test_resolve_pending_deletes_the_record(redis_client: Redis) -> None:
    store = ConfirmationStore(redis_client)
    confirmation_id = await store.create_pending(
        uuid.uuid4(), "echo", {"text": "hi"}, "Вызвать echo"
    )

    await store.resolve_pending(confirmation_id)

    with pytest.raises(NotFoundError):
        await store.get_pending(confirmation_id)


@pytest.mark.integration
async def test_resolve_pending_on_unknown_id_does_not_raise(redis_client: Redis) -> None:
    store = ConfirmationStore(redis_client)

    await store.resolve_pending(uuid.uuid4())


@pytest.mark.integration
async def test_pending_confirmation_expires_after_its_ttl(redis_client: Redis) -> None:
    store = ConfirmationStore(redis_client, ttl_seconds=1)
    confirmation_id = await store.create_pending(
        uuid.uuid4(), "echo", {"text": "hi"}, "Вызвать echo"
    )

    await asyncio.sleep(1.5)

    with pytest.raises(NotFoundError):
        await store.get_pending(confirmation_id)
