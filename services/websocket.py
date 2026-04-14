from fastapi import WebSocket

# ============================================================
# WebSocket 连接管理
# ============================================================

class ConnectionManager:
    def __init__(self):
        self.connections: dict[str, list[WebSocket]] = {}

    async def connect(self, project_id: str, websocket: WebSocket):
        await websocket.accept()
        if project_id not in self.connections:
            self.connections[project_id] = []
        self.connections[project_id].append(websocket)

    def disconnect(self, project_id: str, websocket: WebSocket):
        if project_id in self.connections:
            try:
                self.connections[project_id].remove(websocket)
            except ValueError:
                pass

    async def broadcast(self, project_id: str, message: dict):
        """向项目的所有 WebSocket 连接广播消息"""
        if project_id in self.connections:
            dead = []
            for ws in self.connections[project_id]:
                try:
                    await ws.send_json(message)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                try:
                    self.connections[project_id].remove(ws)
                except ValueError:
                    pass