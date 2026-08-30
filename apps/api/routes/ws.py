from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from libs.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()


@router.websocket("/ws")
async def agent_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    logger.info("ws.connected", client=str(websocket.client))
    try:
        while True:
            message = await websocket.receive_text()
            await websocket.send_json({"echo": message})
    except WebSocketDisconnect:
        logger.info("ws.disconnected", client=str(websocket.client))
