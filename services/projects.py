import os, json, asyncio
from datetime import datetime

from models.projects import WorkflowStage
from services.websocket import ConnectionManager

# 全局项目状态存储
_projects: dict[str, dict] = {}
_review_events: dict[str, asyncio.Event] = {}  # 用于暂停/恢复
_review_decisions: dict[str, dict] = {}  # 用户审核决策

manager = ConnectionManager()

# 项目元数据持久化目录
PROJECTS_META_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "projects_meta"
)
os.makedirs(PROJECTS_META_DIR, exist_ok=True)


def save_project_meta(project_id: str):
    """将项目元数据（不含大字段）持久化到磁盘"""
    try:
        proj = _projects.get(project_id, {})
        meta = {
            "id": proj.get("id", project_id),
            "topic": proj.get("topic", ""),
            "created_at": proj.get("created_at", datetime.now().isoformat()),
            "status": proj.get("status", {}),
            "from_analysis": proj.get("from_analysis"),
            "result_path": proj.get("result", {}).get("final_video")
            if proj.get("result")
            else None,
        }
        path = os.path.join(PROJECTS_META_DIR, f"{project_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[持久化] 保存项目 {project_id} 元数据失败: {e}")


def load_all_project_metas():
    """启动时从磁盘恢复所有项目元数据"""
    try:
        for fname in sorted(os.listdir(PROJECTS_META_DIR)):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(PROJECTS_META_DIR, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                pid = meta.get("id", fname.replace(".json", ""))
                if pid not in _projects:
                    _projects[pid] = {
                        "id": pid,
                        "topic": meta.get("topic", ""),
                        "created_at": meta.get("created_at", ""),
                        "status": meta.get("status", {"stage": "completed", "progress": 100}),
                        "script": None,
                        "result": {"final_video": meta["result_path"]}
                        if meta.get("result_path")
                        else None,
                        "from_analysis": meta.get("from_analysis"),
                        "_restored": True,  # 标记为从磁盘恢复
                    }
            except Exception as e:
                print(f"[持久化] 加载 {fname} 失败: {e}")
        print(f"[持久化] 已从磁盘恢复 {len(_projects)} 个历史项目")
    except Exception as e:
        print(f"[持久化] 加载历史项目失败: {e}")


async def push_status(project_id: str, stage: WorkflowStage, progress: int, message: str, **kwargs):
    """推送工作流状态到前端"""
    status = {
        "type": "status",
        "project_id": project_id,
        "stage": stage.value,
        "progress": progress,
        "message": message,
        "timestamp": datetime.now().isoformat(),
        **kwargs,
    }
    _projects[project_id]["status"] = status
    await manager.broadcast(project_id, status)
    
    
