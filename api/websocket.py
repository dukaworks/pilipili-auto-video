from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from services.projects import manager, _projects


# ============================================================
# WebSocket 端点
# ============================================================

router = APIRouter()


@router.websocket("/ws/{project_id}")
async def websocket_endpoint(websocket: WebSocket, project_id: str):
    """
    WebSocket 连接 - 实时推送工作流状态到前端 Agent Console
    """
    await manager.connect(project_id, websocket)

    # 如果项目已有状态，立即推送（恢复场景）
    if project_id in _projects and _projects[project_id].get("status"):
        try:
            await websocket.send_json(_projects[project_id]["status"])
        except Exception:
            pass

    try:
        while True:
            # 保持连接，接收心跳
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(project_id, websocket)
    except Exception:
        manager.disconnect(project_id, websocket)
