import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# 导入核心模块
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .auth import router as auth_router
from .projects import router as projects_router
from .websocket import router as websocket_router
from services.user import init_auth
from services.projects import load_all_project_metas

# ============================================================
# 应用初始化
# ============================================================

def create_app() -> FastAPI:

    app = FastAPI(title="芝麻开门 Open-Door API", description="全自动 AI 视频生成代理", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册认证路由和项目管理路由
    app.include_router(auth_router)
    app.include_router(projects_router)
    app.include_router(websocket_router)

    init_auth()  # 初始化认证系统

    # 静态文件：用户头像
    DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    AVATARS_DIR = os.path.join(DATA_DIR, "avatars")
    os.makedirs(AVATARS_DIR, exist_ok=True)
    app.mount("/data/avatars", StaticFiles(directory=AVATARS_DIR), name="avatars")

    @app.get("/health", tags=["System"])
    async def health_check():
        return {"status": "ok", "version": "1.0.0", "name": "芝麻开门 Open-Door"}

    @app.on_event("startup")
    async def startup_event():
        """FastAPI 启动时自动恢复历史项目"""
        load_all_project_metas()
        print("应用启动完成，历史项目已加载")
        
    return app