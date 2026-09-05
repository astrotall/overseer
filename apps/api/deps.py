from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.services import ChatService
from libs.core.config import Settings, get_settings
from libs.db.redis import get_redis
from libs.db.repositories import ConversationRepository
from libs.db.session import get_session
from libs.llm.base import LLMClient
from libs.llm.factory import get_llm_client
from libs.tools import ToolRegistry, get_tool_registry

SessionDep = Annotated[AsyncSession, Depends(get_session)]
RedisDep = Annotated[Redis, Depends(get_redis)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
ToolRegistryDep = Annotated[ToolRegistry, Depends(get_tool_registry)]


def get_active_llm_client(settings: SettingsDep) -> LLMClient:
    return get_llm_client(settings)


LLMClientDep = Annotated[LLMClient, Depends(get_active_llm_client)]


def get_chat_service(session: SessionDep, llm_client: LLMClientDep) -> ChatService:
    return ChatService(session, llm_client)


ChatServiceDep = Annotated[ChatService, Depends(get_chat_service)]


def get_conversation_repository(session: SessionDep) -> ConversationRepository:
    return ConversationRepository(session)


ConversationRepositoryDep = Annotated[ConversationRepository, Depends(get_conversation_repository)]

__all__ = [
    "ChatServiceDep",
    "ConversationRepositoryDep",
    "LLMClientDep",
    "RedisDep",
    "SessionDep",
    "SettingsDep",
    "ToolRegistryDep",
    "get_active_llm_client",
    "get_chat_service",
    "get_conversation_repository",
]
