from pydantic import BaseModel
from enum import Enum
from typing import Optional

# ============================================================
# 工作流状态管理
# ============================================================


class WorkflowStage(str, Enum):
    IDLE = "idle"
    GENERATING_SCRIPT = "generating_script"
    AWAITING_REVIEW = "awaiting_review"  # 人工审核关卡 ⬅️ 关键
    GENERATING_IMAGES = "generating_images"
    GENERATING_AUDIO = "generating_audio"
    GENERATING_VIDEO = "generating_video"
    ASSEMBLING = "assembling"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkflowStatus(BaseModel):
    project_id: str
    stage: WorkflowStage
    progress: int  # 0-100
    message: str
    current_scene: Optional[int] = None
    total_scenes: Optional[int] = None
    error: Optional[str] = None
    result: Optional[dict] = None
    
# ============================================================
# 请求/响应模型
# ============================================================


class CreateProjectRequest(BaseModel):
    topic: str
    style: Optional[str] = None
    target_duration: Optional[int] = 60  # 目标时长（秒）
    voice_id: Optional[str] = None
    video_engine: Optional[str] = "kling"  # "kling" / "seedance" / "auto"
    reference_images: Optional[list[str]] = []  # 角色参考图路径
    add_subtitles: bool = True
    auto_publish: bool = False
    preset_scenes: Optional[list[dict]] = None  # 对标分析分镜（有则跳过 LLM 生成）
    preset_title: Optional[str] = None  # 对标分析标题
    resolution: Optional[str] = "1080p"  # 输出分辨率："720p" / "1080p" / "4K"


class ReviewDecisionRequest(BaseModel):
    approved: bool
    scenes: Optional[list[dict]] = None  # 修改后的分镜数据（如果有修改）

# ============================================================
# 更新 API 密钥
# ============================================================

class UpdateApiKeysRequest(BaseModel):
    llm_provider: Optional[str] = None
    llm_api_key: Optional[str] = None
    image_gen_api_key: Optional[str] = None
    tts_api_key: Optional[str] = None
    kling_api_key: Optional[str] = None
    kling_api_secret: Optional[str] = None
    seedance_api_key: Optional[str] = None
    mem0_api_key: Optional[str] = None

# ============================================================
# 测试 API 密钥
# ============================================================


class TestKeyRequest(BaseModel):
    service: str  # llm / image_gen / tts / kling / seedance
    

