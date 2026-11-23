from __future__ import annotations

import uuid
from typing import Final

NAMESPACE: Final = "overseer:executor:"

REQUESTS_KEY: Final = f"{NAMESPACE}requests"
HEARTBEAT_KEY: Final = f"{NAMESPACE}heartbeat"
OWNER_KEY: Final = f"{NAMESPACE}owner"
REPLY_PREFIX: Final = f"{NAMESPACE}reply:"
SEEN_PREFIX: Final = f"{NAMESPACE}seen:"

EXECUTOR_REDIS_USER: Final = "overseer-executor"


def reply_key(request_id: uuid.UUID) -> str:
    return f"{REPLY_PREFIX}{request_id}"


def seen_key(request_id: uuid.UUID) -> str:
    return f"{SEEN_PREFIX}{request_id}"
