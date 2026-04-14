
import os, json, uuid, yaml
import asyncio
from datetime import datetime
from typing import Optional
from pathlib import Path

from fastapi import (
    APIRouter,
    HTTPException,
    BackgroundTasks,
    UploadFile,
    File,
    Form,
    Depends,
)
import subprocess

# 导入核心模块
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import (
    get_config,
    get_active_llm_config,
    reset_config,
)
from services.modules.llm import (
    generate_script_sync,
    VideoScript,
    Scene,
    script_to_dict,
    analyze_reference_video_sync,
    ReferenceVideoAnalysis,
)
from services.modules.image_gen import generate_all_keyframes_sync
from services.modules.tts import generate_all_voiceovers_sync, update_scene_durations
from services.modules.video_gen import generate_all_video_clips_sync, _generate_kling_jwt
from services.modules.assembler import assemble_video, AssemblyPlan
from services.modules.jianying_draft import generate_jianying_draft
from services.modules.memory import get_memory_manager
from api.auth import router as auth_router, get_current_user, TokenData
from services.user import init_auth
from models.projects import (
    WorkflowStage, 
    CreateProjectRequest, 
    ReviewDecisionRequest, 
    UpdateApiKeysRequest, 
    TestKeyRequest
)
from services.projects import (
    save_project_meta, 
    _projects, 
    _review_events, 
    _review_decisions, 
    push_status
)

from services.project_funcs.config_tools import _write_config_updates
from services.project_funcs.core_workflow import run_workflow

router = APIRouter(prefix="/api", tags=["API"])


from services.project_funcs.file_upload import UPLOAD_DIR, VIDEO_UPLOAD_DIR, _extract_frame_from_video

@router.post("/upload/reference")
async def upload_reference_image(
    file: UploadFile = File(...),
):
    """
    上传角色参考图（图片或视频）。
    - 图片：直接保存
    - 视频：自动提取最清晰的一帧
    返回保存后的文件路径，供创建项目时传入 reference_images。
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名为空")

    # 生成唯一文件名
    ext = Path(file.filename).suffix.lower()
    unique_name = f"{uuid.uuid4().hex[:12]}{ext}"
    save_path = os.path.join(UPLOAD_DIR, unique_name)

    # 保存上传文件
    with open(save_path, "wb") as f:
        content = await file.read()
        f.write(content)

    # 判断是否为视频文件
    video_exts = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv"}
    image_exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

    if ext in video_exts:
        # 从视频中提取帧
        frame_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex[:12]}_frame.jpg")
        try:
            _extract_frame_from_video(save_path, frame_path)
            # 删除原视频节省空间
            os.remove(save_path)
            return {
                "path": os.path.abspath(frame_path),
                "filename": os.path.basename(frame_path),
                "type": "video_frame",
                "message": "已从视频中提取参考帧",
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"视频截帧失败: {str(e)}")
    elif ext in image_exts:
        return {
            "path": os.path.abspath(save_path),
            "filename": unique_name,
            "type": "image",
            "message": "参考图已上传",
        }
    else:
        os.remove(save_path)
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: {ext}。支持的格式: {', '.join(image_exts | video_exts)}",
        )

# ============================================================
# API 路由
# ============================================================


@router.post("/projects")
async def create_project(
    request: CreateProjectRequest,
    background_tasks: BackgroundTasks,
    current_user: TokenData = Depends(get_current_user),
):
    """创建新项目，启动视频生成工作流"""
    project_id = str(uuid.uuid4())[:8]

    _projects[project_id] = {
        "id": project_id,
        "user_id": current_user.user_id,  # 关联用户
        "topic": request.topic,
        "created_at": datetime.now().isoformat(),
        "status": {"stage": WorkflowStage.IDLE.value, "progress": 0},
        "script": None,
        "result": None,
    }

    save_project_meta(project_id)
    background_tasks.add_task(run_workflow, project_id, request)

    return {"project_id": project_id, "message": "工作流已启动"}


@router.get("/projects/{project_id}")
async def get_project(project_id: str, current_user: TokenData = Depends(get_current_user)):
    """获取项目状态"""
    if project_id not in _projects:
        raise HTTPException(status_code=404, detail="项目不存在")

    # 检查用户权限
    project = _projects[project_id]
    if project.get("user_id") and project.get("user_id") != current_user.user_id:
        raise HTTPException(status_code=403, detail="无权访问此项目")

    return project


@router.get("/projects")
async def list_projects(current_user: TokenData = Depends(get_current_user)):
    """获取当前用户的所有项目列表"""
    user_projects = [p for p in _projects.values() if p.get("user_id") == current_user.user_id]
    return user_projects


@router.post("/projects/{project_id}/review")
async def submit_review(
    project_id: str,
    decision: ReviewDecisionRequest,
    current_user: TokenData = Depends(get_current_user),
):
    """
    提交脚本/分镜审核决策

    这是人工审核关卡的核心接口：
    - approved=true + scenes=修改后的数据 → 继续工作流
    - approved=false → 取消工作流
    """
    # 检查用户权限
    if project_id not in _projects:
        raise HTTPException(status_code=404, detail="项目不存在")
    project = _projects[project_id]
    if project.get("user_id") and project.get("user_id") != current_user.user_id:
        raise HTTPException(status_code=403, detail="无权访问此项目")

    if project_id not in _review_events:
        raise HTTPException(status_code=400, detail="该项目当前不在审核状态")

    _review_decisions[project_id] = {
        "approved": decision.approved,
        "scenes": [s for s in decision.scenes] if decision.scenes else None,
    }

    # 触发工作流继续
    _review_events[project_id].set()

    return {"message": "审核决策已提交", "approved": decision.approved}


@router.put("/projects/{project_id}/script")
async def update_script(
    project_id: str, scenes: list[dict], current_user: TokenData = Depends(get_current_user)
):
    """实时更新分镜内容（在审核界面编辑时调用）"""
    # 检查用户权限
    if project_id not in _projects:
        raise HTTPException(status_code=404, detail="项目不存在")
    project = _projects[project_id]
    if project.get("user_id") and project.get("user_id") != current_user.user_id:
        raise HTTPException(status_code=403, detail="无权访问此项目")

    if _projects[project_id]["script"]:
        _projects[project_id]["script"]["scenes"] = scenes

    return {"message": "分镜已更新"}


@router.get("/projects/{project_id}/download")
async def get_download_links(project_id: str, current_user: TokenData = Depends(get_current_user)):
    """获取成品视频和剪映草稿的下载链接"""
    if project_id not in _projects:
        raise HTTPException(status_code=404, detail="项目不存在")

    # 检查用户权限
    project = _projects[project_id]
    if project.get("user_id") and project.get("user_id") != current_user.user_id:
        raise HTTPException(status_code=403, detail="无权访问此项目")

    result = _projects[project_id].get("result")
    if not result:
        raise HTTPException(status_code=400, detail="项目尚未完成")

    return {
        "final_video": result.get("final_video"),
        "draft_dir": result.get("draft_dir"),
        "total_duration": result.get("total_duration"),
    }


@router.post("/settings/keys")
async def update_api_keys(request: UpdateApiKeysRequest):
    """
    更新 API Keys 配置
    将 Keys 写入 config.yaml 并重置内存配置单例，立即生效
    """
    # 构建需要写入的更新
    updates = {}

    if request.llm_provider:
        updates["llm.default_provider"] = request.llm_provider

    # LLM api_key 写入当前激活的 provider 下
    if request.llm_api_key:
        config = get_config()
        provider = config.llm.default_provider
        updates[f"llm.{provider}.api_key"] = request.llm_api_key

    if request.image_gen_api_key:
        updates["image_gen.api_key"] = request.image_gen_api_key

    if request.tts_api_key:
        updates["tts.minimax.api_key"] = request.tts_api_key

    if request.kling_api_key:
        updates["video_gen.kling.api_key"] = request.kling_api_key

    if request.kling_api_secret:
        updates["video_gen.kling.api_secret"] = request.kling_api_secret

    if request.seedance_api_key:
        updates["video_gen.seedance.api_key"] = request.seedance_api_key

    if request.mem0_api_key:
        updates["memory.mem0_api_key"] = request.mem0_api_key

    if updates:
        try:
            _write_config_updates(updates)
            # 重置配置单例，让下次请求重新加载
            reset_config()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"配置写入失败: {str(e)}")

    return {"message": "API Keys 已更新并写入配置文件", "updated_keys": list(updates.keys())}


@router.post("/projects/{project_id}/resume")
async def resume_project(
    project_id: str,
    background_tasks: BackgroundTasks,
    current_user: TokenData = Depends(get_current_user),
    video_engine: str = "kling",
    add_subtitles: bool = True,
):
    """
    断点续传：从已有的 keyframes + audio 文件直接跳到视频生成阶段。
    适用于图片/TTS 已生成但视频生成失败的项目。
    """
    config = get_config()
    project_dir = os.path.join(config.local.output_dir, project_id)
    script_path = os.path.join(project_dir, "script.json")

    if not os.path.exists(script_path):
        raise HTTPException(status_code=404, detail=f"项目 {project_id} 不存在或 script.json 缺失")

    # 检查用户权限（如果是已有项目）
    existing_project = _projects.get(project_id)
    if existing_project:
        if (
            existing_project.get("user_id")
            and existing_project.get("user_id") != current_user.user_id
        ):
            raise HTTPException(status_code=403, detail="无权访问此项目")
    else:
        # 新项目，关联用户
        existing_project = {"user_id": current_user.user_id}

    # 注册到 _projects
    with open(script_path, "r", encoding="utf-8") as f:
        script_dict = json.load(f)

    _projects[project_id] = {
        "id": project_id,
        "user_id": current_user.user_id,  # 关联用户
        "topic": script_dict.get("topic", script_dict.get("title", "")),
        "created_at": datetime.now().isoformat(),
        "status": {"stage": WorkflowStage.GENERATING_VIDEO.value, "progress": 50},
        "script": script_dict,
        "result": None,
    }
    save_project_meta(project_id)

    background_tasks.add_task(run_resume_workflow, project_id, video_engine, add_subtitles)
    return {"project_id": project_id, "message": "断点续传已启动，从视频生成阶段继续"}


async def run_resume_workflow(
    project_id: str, video_engine: str = "kling", add_subtitles: bool = True
):
    """从已有 keyframes + audio 文件断点续传，直接跳到视频生成阶段"""
    from services.modules.image_gen import reset_failed_models

    reset_failed_models()

    config = get_config()
    project_dir = os.path.join(config.local.output_dir, project_id)

    try:
        # 读取脚本
        script_path = os.path.join(project_dir, "script.json")
        with open(script_path, "r", encoding="utf-8") as f:
            script_dict = json.load(f)

        from services.modules.llm import VideoScript, Scene

        scenes = [Scene(**s) for s in script_dict["scenes"]]
        script = VideoScript(
            title=script_dict["title"],
            topic=script_dict.get("topic", script_dict["title"]),
            style=script_dict.get("style", ""),
            total_duration=script_dict.get("total_duration", 0),
            scenes=scenes,
            characters=script_dict.get("characters", []),
            metadata=script_dict.get("metadata", {}),
        )

        await push_status(
            project_id,
            WorkflowStage.GENERATING_VIDEO,
            50,
            f"断点续传：读取已有关键帧和配音，共 {len(scenes)} 个分镜...",
        )

        # 扫描已有 keyframes
        keyframes_dir = os.path.join(project_dir, "keyframes")
        keyframe_paths: dict[int, str] = {}
        if os.path.exists(keyframes_dir):
            for fname in os.listdir(keyframes_dir):
                if fname.startswith("scene_") and fname.endswith(
                    ("_keyframe.png", "_keyframe.jpg")
                ):
                    try:
                        scene_id = int(fname.split("_")[1])
                        keyframe_paths[scene_id] = os.path.join(keyframes_dir, fname)
                    except (ValueError, IndexError):
                        pass

        # 扫描已有 audio，同时更新 scene duration
        audio_dir = os.path.join(project_dir, "audio")
        audio_paths: dict[int, str] = {}
        from services.modules.tts import get_audio_duration, update_scene_durations

        voiceover_results: dict[int, tuple[str, float]] = {}
        if os.path.exists(audio_dir):
            for fname in os.listdir(audio_dir):
                if fname.startswith("scene_") and fname.endswith("_voiceover.mp3"):
                    try:
                        scene_id = int(fname.split("_")[1])
                        fpath = os.path.join(audio_dir, fname)
                        dur = get_audio_duration(fpath)
                        audio_paths[scene_id] = fpath
                        voiceover_results[scene_id] = (fpath, dur)
                    except (ValueError, IndexError):
                        pass

        # 用 TTS 时长更新分镜 duration
        if voiceover_results:
            script.scenes = update_scene_durations(script.scenes, voiceover_results)

        missing_kf = [s.scene_id for s in script.scenes if s.scene_id not in keyframe_paths]
        if missing_kf:
            await push_status(
                project_id,
                WorkflowStage.FAILED,
                0,
                f"缺少分镜 {missing_kf} 的关键帧图片，无法续传",
                error=f"keyframes missing: {missing_kf}",
            )
            return

        await push_status(
            project_id,
            WorkflowStage.GENERATING_VIDEO,
            55,
            f"已加载 {len(keyframe_paths)} 张关键帧、{len(audio_paths)} 段配音，开始生成视频片段...",
            keyframes=list(keyframe_paths.values()),
        )

        # ── 视频生成 ──────────────────────────────────────────
        clips_dir = os.path.join(project_dir, "clips")
        engine = None if video_engine == "auto" else video_engine
        auto_route = video_engine == "auto"

        video_clips = await asyncio.to_thread(
            generate_all_video_clips_sync,
            scenes=script.scenes,
            keyframe_paths=keyframe_paths,
            output_dir=clips_dir,
            engine=engine,
            auto_route=auto_route,
            config=config,
            verbose=True,
            resolution=request.resolution or "1080p",
        )

        await push_status(
            project_id, WorkflowStage.ASSEMBLING, 80, "视频片段生成完成，开始组装最终成片..."
        )

        # ── 组装拼接 ──────────────────────────────────────────
        output_dir = os.path.join(project_dir, "output")
        temp_dir = os.path.join(project_dir, "temp")
        safe_title = "".join(c for c in script.title if c not in r'\/:*?"<>|').strip() or "output"
        final_video = os.path.join(output_dir, f"{safe_title}.mp4")
        os.makedirs(output_dir, exist_ok=True)

        plan = AssemblyPlan(
            scenes=script.scenes,
            video_clips=video_clips,
            audio_clips=audio_paths,
            output_path=final_video,
            temp_dir=temp_dir,
            add_subtitles=add_subtitles,
        )
        await asyncio.to_thread(assemble_video, plan, True)

        # 剪映草稿
        draft_dir = os.path.join(output_dir, "jianying_draft")
        await asyncio.to_thread(
            generate_jianying_draft,
            script=script,
            video_clips=video_clips,
            audio_clips=audio_paths,
            output_dir=draft_dir,
            project_name=safe_title,
            verbose=True,
        )

        result = {
            "final_video": final_video,
            "draft_dir": draft_dir,
            "script": script_dict,
            "total_duration": sum(s.duration for s in script.scenes),
        }
        _projects[project_id]["result"] = result

        await push_status(
            project_id,
            WorkflowStage.COMPLETED,
            100,
            f"🎉 视频生成完成！《{script.title}》",
            result=result,
        )
        save_project_meta(project_id)

    except Exception as e:
        import traceback

        error_msg = f"{type(e).__name__}: {str(e)}"
        await push_status(
            project_id,
            WorkflowStage.FAILED,
            0,
            f"续传工作流执行失败: {error_msg}",
            error=traceback.format_exc(),
        )
        save_project_meta(project_id)


@router.get("/settings/keys/status")
async def get_keys_status():
    """检查各 API Key 的配置状态"""
    config = get_config()
    active_llm = get_active_llm_config(config)
    return {
        "llm": {
            "provider": config.llm.default_provider,
            "configured": bool(active_llm.api_key),
        },
        "image_gen": {
            "provider": "nano_banana",
            "configured": bool(config.image_gen.api_key),
        },
        "tts": {
            "provider": "minimax",
            "configured": bool(config.tts.api_key),
        },
        "kling": {
            "configured": bool(
                config.video_gen.kling.api_key and config.video_gen.kling.api_secret
            ),
        },
        "seedance": {
            "configured": bool(config.video_gen.seedance.api_key),
        },
    }


@router.post("/settings/keys/test")
async def test_api_key(request: TestKeyRequest):
    """
    测试指定服务的 API Key 是否有效。
    对每个服务发送一个最小化请求来验证 Key 的有效性。
    """
    config = get_config()
    service = request.service

    try:
        if service == "llm":
            active_llm = get_active_llm_config(config)
            if not active_llm.api_key:
                return {"success": False, "message": "API Key 未配置"}
            provider = config.llm.default_provider
            if provider == "gemini":
                from openai import AsyncOpenAI

                client = AsyncOpenAI(
                    api_key=active_llm.api_key,
                    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                )
            else:
                from openai import AsyncOpenAI

                client = AsyncOpenAI(
                    api_key=active_llm.api_key,
                    base_url=active_llm.base_url or "https://api.openai.com/v1",
                )
            resp = await client.chat.completions.create(
                model=active_llm.model,
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=1,
            )
            return {"success": True, "message": f"{provider} 连接成功，模型: {active_llm.model}"}

        elif service == "image_gen":
            if not config.image_gen.api_key:
                return {"success": False, "message": "API Key 未配置"}
            from google import genai

            client = genai.Client(api_key=config.image_gen.api_key)
            models = list(client.models.list())
            return {"success": True, "message": f"Gemini API 连接成功，可用模型 {len(models)} 个"}

        elif service == "tts":
            if not config.tts.api_key:
                return {"success": False, "message": "API Key 未配置"}
            import aiohttp

            url = "https://api.minimax.chat/v1/t2a_v2"
            headers = {
                "Authorization": f"Bearer {config.tts.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": config.tts.model or "speech-02-hd",
                "text": "测试",
                "stream": False,
                "voice_setting": {
                    "voice_id": config.tts.default_voice or "female-shaonv",
                    "speed": 1.0,
                    "vol": 1.0,
                    "pitch": 0,
                },
                "audio_setting": {
                    "sample_rate": 32000,
                    "format": "mp3",
                },
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    result = await resp.json()
            if "base_resp" in result:
                code = result["base_resp"].get("status_code", -1)
                msg = result["base_resp"].get("status_msg", "未知错误")
                if code == 0:
                    return {"success": True, "message": "MiniMax TTS 连接成功"}
                return {"success": False, "message": f"MiniMax 返回错误: {msg} (code={code})"}
            return {
                "success": False,
                "message": f"MiniMax 返回异常: {json.dumps(result, ensure_ascii=False)[:200]}",
            }

        elif service == "kling":
            if not config.video_gen.kling.api_key or not config.video_gen.kling.api_secret:
                return {"success": False, "message": "API Key 或 API Secret 未配置"}
            import aiohttp

            token = _generate_kling_jwt(
                config.video_gen.kling.api_key, config.video_gen.kling.api_secret
            )
            url = f"{config.video_gen.kling.base_url}/v1/videos/image2video"
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json={}, headers=headers) as resp:
                    status = resp.status
            if status == 401 or status == 403:
                return {
                    "success": False,
                    "message": f"Kling 认证失败 (HTTP {status})，请检查 API Key 和 Secret",
                }
            return {"success": True, "message": "Kling API 认证成功"}

        elif service == "seedance":
            if not config.video_gen.seedance.api_key:
                return {"success": False, "message": "API Key 未配置"}
            import aiohttp

            url = f"{config.video_gen.seedance.base_url}/contents/generations/tasks"
            headers = {
                "Authorization": f"Bearer {config.video_gen.seedance.api_key}",
                "Content-Type": "application/json",
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json={}, headers=headers) as resp:
                    status = resp.status
            if status == 401 or status == 403:
                return {
                    "success": False,
                    "message": f"Seedance 认证失败 (HTTP {status})，请检查 API Key",
                }
            return {"success": True, "message": "Seedance API 认证成功"}

        else:
            return {"success": False, "message": f"未知服务: {service}"}

    except Exception as e:
        return {"success": False, "message": f"连接失败: {type(e).__name__}: {str(e)}"}


# ============================================================
# 对标视频分析 API（P1 新增）
# ============================================================

# 内存缓存：对标视频分析结果（project_id → analysis）
_reference_analyses: dict[str, dict] = {}


def _analysis_to_dict(analysis: ReferenceVideoAnalysis) -> dict:
    """将 ReferenceVideoAnalysis 转为可序列化字典"""
    return {
        "title": analysis.title,
        "style": analysis.style,
        "aspect_ratio": analysis.aspect_ratio,
        "total_duration": analysis.total_duration,
        "bgm_style": analysis.bgm_style,
        "color_grade": analysis.color_grade,
        "overall_prompt": analysis.overall_prompt,
        "characters": [
            {
                "character_id": c.character_id,
                "name": c.name,
                "description": c.description,
                "appearance_prompt": c.appearance_prompt,
                "replacement_image": c.replacement_image,
            }
            for c in analysis.characters
        ],
        "scenes": [
            {
                "scene_id": s.scene_id,
                "duration": s.duration,
                "image_prompt": s.image_prompt,
                "video_prompt": s.video_prompt,
                "voiceover": s.voiceover,
                "shot_mode": s.shot_mode,
                "transition": s.transition,
                "camera_motion": s.camera_motion,
                "style_tags": s.style_tags,
            }
            for s in analysis.scenes
        ],
        "reverse_prompts": analysis.reverse_prompts,
        "raw_analysis": analysis.raw_analysis,
    }


@router.post("/analyze/upload")
async def analyze_reference_video_upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    """
    上传对标视频文件，触发 Gemini 分析

    分析结果包含：
    - 人物列表（外貌描述 + 英文提示词）
    - 分镜结构（含 shot_mode 标注）
    - 每个分镜的反推提示词（reverse_prompt）
    - 整体风格提示词

    返回 analysis_id，前端通过 GET /api/analyze/{analysis_id} 轮询结果
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名为空")

    ext = Path(file.filename).suffix.lower()
    video_exts = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv"}
    if ext not in video_exts:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: {ext}。支持的视频格式: {', '.join(video_exts)}",
        )

    # 保存上传文件
    analysis_id = uuid.uuid4().hex[:12]
    save_path = os.path.join(VIDEO_UPLOAD_DIR, f"{analysis_id}{ext}")

    with open(save_path, "wb") as f:
        content = await file.read()
        f.write(content)

    # 初始化分析状态
    _reference_analyses[analysis_id] = {
        "analysis_id": analysis_id,
        "status": "processing",
        "filename": file.filename,
        "file_path": save_path,
        "created_at": datetime.now().isoformat(),
        "result": None,
        "error": None,
    }

    # 后台异步执行分析
    background_tasks.add_task(_run_reference_analysis, analysis_id, save_path)

    return {
        "analysis_id": analysis_id,
        "status": "processing",
        "message": "视频已上传，正在分析中...",
    }


async def _run_reference_analysis(analysis_id: str, video_path: str):
    """后台任务：执行对标视频分析"""
    try:
        config = get_config()
        analysis = (
            await __import__("asyncio")
            .get_event_loop()
            .run_in_executor(
                None, lambda: analyze_reference_video_sync(video_path, config, verbose=True)
            )
        )
        _reference_analyses[analysis_id]["status"] = "completed"
        _reference_analyses[analysis_id]["result"] = _analysis_to_dict(analysis)
    except Exception as e:
        import traceback

        _reference_analyses[analysis_id]["status"] = "failed"
        _reference_analyses[analysis_id]["error"] = (
            f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}"
        )


@router.get("/analyze/{analysis_id}")
async def get_reference_analysis(analysis_id: str):
    """
    获取对标视频分析结果

    status 字段：
    - processing：分析中
    - completed：分析完成，result 字段包含完整结果
    - failed：分析失败，error 字段包含错误信息
    """
    if analysis_id not in _reference_analyses:
        raise HTTPException(status_code=404, detail="分析任务不存在")
    return _reference_analyses[analysis_id]


@router.post("/analyze/{analysis_id}/replace-character")
async def replace_character(
    analysis_id: str,
    character_id: int = Form(...),
    file: UploadFile = File(...),
):
    """
    为对标视频中的某个人物上传替换参考图

    上传后，该人物的 replacement_image 字段将被更新。
    创建新项目时可以将此路径传入 reference_images，
    实现人物替换（用用户上传的人物替换对标视频中的原始人物）。
    """
    if analysis_id not in _reference_analyses:
        raise HTTPException(status_code=404, detail="分析任务不存在")

    analysis_data = _reference_analyses[analysis_id]
    if analysis_data["status"] != "completed" or not analysis_data.get("result"):
        raise HTTPException(status_code=400, detail="分析尚未完成")

    ext = Path(file.filename).suffix.lower()
    image_exts = {".jpg", ".jpeg", ".png", ".webp"}
    video_exts = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

    unique_name = f"{uuid.uuid4().hex[:12]}{ext}"
    save_path = os.path.join(UPLOAD_DIR, unique_name)

    with open(save_path, "wb") as f:
        content = await file.read()
        f.write(content)

    # 如果是视频，提取帧
    if ext in video_exts:
        frame_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex[:12]}_frame.jpg")
        try:
            _extract_frame_from_video(save_path, frame_path)
            os.remove(save_path)
            save_path = frame_path
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"视频截帧失败: {str(e)}")

    # 更新人物的 replacement_image
    characters = analysis_data["result"]["characters"]
    for char in characters:
        if char["character_id"] == character_id:
            char["replacement_image"] = os.path.abspath(save_path)
            return {
                "message": f"人物 {char['name']} 的替换参考图已更新",
                "character_id": character_id,
                "replacement_image": os.path.abspath(save_path),
                "path": os.path.abspath(save_path),
            }

    raise HTTPException(status_code=404, detail=f"人物 ID {character_id} 不存在")


@router.delete("/analyze/{analysis_id}/remove-character-image")
async def remove_character_image(
    analysis_id: str,
    character_id: int,
):
    """
    删除某个人物的替换参考图（允许用户重新选择或不替换）
    """
    if analysis_id not in _reference_analyses:
        raise HTTPException(status_code=404, detail="分析任务不存在")
    analysis_data = _reference_analyses[analysis_id]
    if analysis_data["status"] != "completed" or not analysis_data.get("result"):
        raise HTTPException(status_code=400, detail="分析尚未完成")
    characters = analysis_data["result"]["characters"]
    for char in characters:
        if char["character_id"] == character_id:
            old_path = char.get("replacement_image")
            char["replacement_image"] = None
            # 尝试删除本地文件
            if old_path and os.path.exists(old_path):
                try:
                    os.remove(old_path)
                except Exception:
                    pass
            return {
                "message": f"人物 {char['name']} 的替换参考图已删除",
                "character_id": character_id,
            }
    raise HTTPException(status_code=404, detail=f"人物 ID {character_id} 不存在")


@router.post("/analyze/{analysis_id}/create-project")
async def create_project_from_analysis(
    analysis_id: str,
    background_tasks: BackgroundTasks,
    topic: Optional[str] = Form(None),
    video_engine: Optional[str] = Form("kling"),
    add_subtitles: bool = Form(True),
):
    """
    基于对标视频分析结果直接创建新项目

    自动将分析出的：
    - 整体风格提示词作为 style
    - 人物替换参考图作为 reference_images
    - 分镜结构作为脚本初稿（跳过 LLM 生成，直接进入审核关卡）
    """
    if analysis_id not in _reference_analyses:
        raise HTTPException(status_code=404, detail="分析任务不存在")

    analysis_data = _reference_analyses[analysis_id]
    if analysis_data["status"] != "completed" or not analysis_data.get("result"):
        raise HTTPException(status_code=400, detail="分析尚未完成")

    result = analysis_data["result"]

    # 收集替换参考图
    reference_images = []
    for char in result["characters"]:
        if char.get("replacement_image") and os.path.exists(char["replacement_image"]):
            reference_images.append(char["replacement_image"])

    # 构建创建请求（将分析分镜直接作为 preset_scenes，跳过 LLM 生成）
    req = CreateProjectRequest(
        topic=topic or result["title"],
        style=result.get("overall_prompt", result.get("style", "")),
        video_engine=video_engine or "kling",
        reference_images=reference_images,
        add_subtitles=add_subtitles,
        preset_scenes=result.get("scenes", []),
        preset_title=result.get("title"),
    )

    project_id = str(uuid.uuid4())[:8]
    _projects[project_id] = {
        "id": project_id,
        "topic": req.topic,
        "created_at": datetime.now().isoformat(),
        "status": {"stage": WorkflowStage.IDLE.value, "progress": 0},
        "script": None,
        "result": None,
        "from_analysis": analysis_id,
    }

    save_project_meta(project_id)
    background_tasks.add_task(run_workflow, project_id, req)

    return {
        "project_id": project_id,
        "message": "已基于对标视频分析创建新项目，工作流已启动",
        "reference_images_count": len(reference_images),
    }


@router.post("/projects/{project_id}/feedback")
async def submit_feedback(project_id: str, rating: int):
    """提交项目评分（1-5星），用于记忆系统学习"""
    config = get_config()
    memory = get_memory_manager(config)
    memory.learn_from_rating(project_id, rating)
    return {"message": f"评分 {rating} 星已记录，记忆系统已更新"}


# ============================================================
# 文件下载端点
# ============================================================

from fastapi.responses import FileResponse
import zipfile
import tempfile


@router.get("/projects/{project_id}/download/video")
async def download_video(project_id: str):
    """直接下载成品视频文件"""
    if project_id not in _projects:
        raise HTTPException(status_code=404, detail="项目不存在")

    result = _projects[project_id].get("result")
    if not result:
        raise HTTPException(status_code=400, detail="项目尚未完成")

    video_path = result.get("final_video", "")
    if not video_path or not os.path.exists(video_path):
        raise HTTPException(status_code=404, detail=f"视频文件不存在: {video_path}")

    filename = os.path.basename(video_path)
    return FileResponse(
        path=video_path,
        media_type="video/mp4",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/projects/{project_id}/download/draft")
async def download_draft(project_id: str):
    """下载剪映草稿文件夹（打包为 zip）"""
    if project_id not in _projects:
        raise HTTPException(status_code=404, detail="项目不存在")

    result = _projects[project_id].get("result")
    if not result:
        raise HTTPException(status_code=400, detail="项目尚未完成")

    draft_dir = result.get("draft_dir", "")
    if not draft_dir or not os.path.exists(draft_dir):
        raise HTTPException(status_code=404, detail=f"草稿目录不存在: {draft_dir}")

    # 打包为 zip
    zip_path = os.path.join(os.path.dirname(draft_dir), "jianying_draft.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(draft_dir):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, os.path.dirname(draft_dir))
                zf.write(file_path, arcname)

    return FileResponse(
        path=zip_path,
        media_type="application/zip",
        filename="jianying_draft.zip",
        headers={"Content-Disposition": 'attachment; filename="jianying_draft.zip"'},
    )
