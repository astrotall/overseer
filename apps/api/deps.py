from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from libs.core.config import Settings, get_settings
from libs.db.redis import get_redis
from libs.db.session import get_session

SessionDep = Annotated[AsyncSession, Depends(get_session)]
RedisDep = Annotated[Redis, Depends(get_redis)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

__all__ = ["RedisDep", "SessionDep", "SettingsDep"]
