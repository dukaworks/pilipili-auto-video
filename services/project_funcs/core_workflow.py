import os, json
import asyncio

from core.config import get_config
from services.modules.llm import (
    generate_script_sync,
    VideoScript,
    Scene,
    script_to_dict,
)
from services.modules.image_gen import generate_all_keyframes_sync
from services.modules.tts import generate_all_voiceovers_sync, update_scene_durations
from services.modules.video_gen import generate_all_video_clips_sync
from services.modules.assembler import assemble_video, AssemblyPlan
from services.modules.jianying_draft import generate_jianying_draft
from services.modules.memory import get_memory_manager
from models.projects import (
    WorkflowStage, 
    CreateProjectRequest, 
)
from services.projects import (
    save_project_meta, 
    _projects, 
    _review_events, 
    _review_decisions, 
    push_status
)

from services.project_funcs.config_tools import _write_config_updates

# ============================================================
# 核心工作流（后台任务）
# ============================================================


async def run_workflow(project_id: str, request: CreateProjectRequest):
    """
    完整的 5 阶段视频生成工作流

    阶段 1: LLM 生成脚本
    阶段 2: 人工审核（暂停，等待用户确认）⬅️ 关键关卡
    阶段 3: 并行生成关键帧图片 + TTS 配音
    阶段 4: 图生视频
    阶段 5: 组装拼接 + 生成剪映草稿
    """
    # 每次新任务开始时重置图像模型黑名单，避免上次任务的失败影响本次
    from services.modules.image_gen import reset_failed_models

    reset_failed_models()

    config = get_config()
    memory = get_memory_manager(config)
    project_dir = os.path.join(config.local.output_dir, project_id)
    os.makedirs(project_dir, exist_ok=True)

    try:
        # ── 阶段 1：生成脚本（或直接使用对标分析分镜）────────────
        if request.preset_scenes:
            # 对标分析模式：直接将分析分镜转换为 VideoScript，跳过 LLM
            await push_status(
                project_id,
                WorkflowStage.GENERATING_SCRIPT,
                5,
                "使用对标视频分析分镜，跳过 LLM 生成...",
            )
            preset_scene_objs = []
            for i, sd in enumerate(request.preset_scenes):
                # 兼容 voiceover_text 和 voiceover 两种字段名（对标分析返回 voiceover_text）
                voiceover_val = sd.get("voiceover") or sd.get("voiceover_text") or ""
                preset_scene_objs.append(
                    Scene(
                        scene_id=sd.get("scene_id") or (i + 1),
                        duration=float(sd.get("duration") or 5),
                        image_prompt=sd.get("image_prompt") or "",
                        video_prompt=sd.get("video_prompt") or "",
                        voiceover=voiceover_val,
                        transition=sd.get("transition") or "crossfade",
                        camera_motion=sd.get("camera_motion") or "static",
                        style_tags=sd.get("style_tags") or [],
                        shot_mode=sd.get("shot_mode"),
                    )
                )
            script = VideoScript(
                title=request.preset_title or request.topic,
                topic=request.topic,
                style=request.style or "",
                total_duration=sum(s.duration for s in preset_scene_objs),
                scenes=preset_scene_objs,
                metadata={},
            )
        else:
            # 普通模式：LLM 生成脚本
            await push_status(
                project_id, WorkflowStage.GENERATING_SCRIPT, 5, "正在分析主题，生成视频脚本..."
            )
            memory_context = memory.build_context_for_generation(request.topic)
            script = await asyncio.to_thread(
                generate_script_sync,
                topic=request.topic,
                style=request.style,
                duration_hint=request.target_duration or 60,
                memory_context=memory_context,
                config=config,
            )

        # 保存脚本到项目
        script_path = os.path.join(project_dir, "script.json")
        script_dict = script_to_dict(script)
        with open(script_path, "w", encoding="utf-8") as f:
            json.dump(script_dict, f, ensure_ascii=False, indent=2)

        _projects[project_id]["script"] = script_dict

        await push_status(
            project_id,
            WorkflowStage.GENERATING_SCRIPT,
            15,
            f"脚本就绪：《{script.title}》，共 {len(script.scenes)} 个分镜",
            script=script_dict,
        )

        # 从脚本中学习风格偏好
        memory.learn_from_script(script_dict, project_id)

        # ── 阶段 2：人工审核关卡 ──────────────────────────────
        await push_status(
            project_id,
            WorkflowStage.AWAITING_REVIEW,
            20,
            "脚本已生成，请审核并确认分镜内容后继续",
            script=script_to_dict(script),
            requires_action=True,
            action_type="review_script",
        )

        # 创建等待事件，暂停工作流
        review_event = asyncio.Event()
        _review_events[project_id] = review_event

        # 等待用户审核（最长等待 30 分钟）
        try:
            await asyncio.wait_for(review_event.wait(), timeout=1800)
        except asyncio.TimeoutError:
            await push_status(
                project_id, WorkflowStage.FAILED, 20, "审核超时（30分钟），工作流已取消"
            )
            return

        # 获取审核决策
        decision = _review_decisions.get(project_id, {})
        if not decision.get("approved", False):
            await push_status(project_id, WorkflowStage.IDLE, 0, "用户取消了工作流")
            return

        # 如果用户修改了分镜，更新脚本
        if decision.get("scenes"):
            updated_scenes = []
            for scene_data in decision["scenes"]:
                # 防止前端传来的 null 字段导致 None.strip() 崩溃
                safe_data = dict(scene_data)
                safe_data["voiceover"] = safe_data.get("voiceover") or ""
                safe_data["image_prompt"] = safe_data.get("image_prompt") or ""
                safe_data["video_prompt"] = safe_data.get("video_prompt") or ""
                safe_data["transition"] = safe_data.get("transition") or "crossfade"
                safe_data["camera_motion"] = safe_data.get("camera_motion") or "static"
                safe_data["style_tags"] = safe_data.get("style_tags") or []
                scene = Scene(**safe_data)
                updated_scenes.append(scene)
            script.scenes = updated_scenes

            # 记录用户修改（隐式学习）
            original_scenes = {
                s["scene_id"]: s for s in (_projects[project_id]["script"] or {}).get("scenes", [])
            }
            for scene in updated_scenes:
                orig = original_scenes.get(scene.scene_id, {})
                if scene.image_prompt != orig.get("image_prompt", ""):
                    memory.learn_from_user_edit(
                        project_id,
                        scene.scene_id,
                        "image_prompt",
                        orig.get("image_prompt", ""),
                        scene.image_prompt,
                    )

        # ── 阶段 3：并行生成关键帧 + TTS ─────────────────────
        await push_status(
            project_id,
            WorkflowStage.GENERATING_IMAGES,
            25,
            f"开始并行生成 {len(script.scenes)} 个分镜关键帧和配音...",
        )

        images_dir = os.path.join(project_dir, "keyframes")
        audio_dir = os.path.join(project_dir, "audio")

        # 并行执行生图和 TTS
        keyframe_task = asyncio.to_thread(
            generate_all_keyframes_sync,
            scenes=script.scenes,
            output_dir=images_dir,
            reference_images=request.reference_images or [],
            characters=script.characters or [],
            config=config,
            verbose=True,
        )

        audio_task = asyncio.to_thread(
            generate_all_voiceovers_sync,
            scenes=script.scenes,
            output_dir=audio_dir,
            voice_id=request.voice_id,
            characters=script.characters or [],
            config=config,
            max_concurrent=2,  # 降低并发数，减少 MiniMax RPM 限速
            verbose=True,
        )

        await push_status(
            project_id, WorkflowStage.GENERATING_AUDIO, 30, "并行生成关键帧图片和配音中..."
        )

        keyframe_paths, voiceover_results = await asyncio.gather(keyframe_task, audio_task)

        # 根据 TTS 时长更新分镜 duration
        script.scenes = update_scene_durations(script.scenes, voiceover_results)
        audio_paths = {sid: path for sid, (path, _) in voiceover_results.items()}

        await push_status(
            project_id,
            WorkflowStage.GENERATING_IMAGES,
            50,
            "关键帧和配音生成完成，开始生成视频片段...",
            keyframes=list(keyframe_paths.values()),
        )

        # ── 阶段 4：图生视频 ──────────────────────────────────
        video_engine = request.video_engine or "kling"
        await push_status(
            project_id,
            WorkflowStage.GENERATING_VIDEO,
            55,
            f"使用 {video_engine.upper()} 生成视频片段...",
        )

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

        # ── 阶段 5：组装拼接 ──────────────────────────────────
        output_dir = os.path.join(project_dir, "output")
        temp_dir = os.path.join(project_dir, "temp")
        # 清理文件名中的非法字符（Windows 兼容）
        safe_title = "".join(c for c in script.title if c not in r'\/:*?"<>|').strip() or "output"
        final_video = os.path.join(output_dir, f"{safe_title}.mp4")
        os.makedirs(output_dir, exist_ok=True)

        plan = AssemblyPlan(
            scenes=script.scenes,
            video_clips=video_clips,
            audio_clips=audio_paths,
            output_path=final_video,
            temp_dir=temp_dir,
            add_subtitles=request.add_subtitles,
        )

        await asyncio.to_thread(assemble_video, plan, True)

        # 生成剪映草稿
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

        # 完成
        result = {
            "final_video": final_video,
            "draft_dir": draft_dir,
            "script": script_to_dict(script),
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
        save_project_meta(project_id)  # 完成时持久化最终状态

    except Exception as e:
        import traceback

        error_msg = f"{type(e).__name__}: {str(e)}"
        await push_status(
            project_id,
            WorkflowStage.FAILED,
            0,
            f"工作流执行失败: {error_msg}",
            error=traceback.format_exc(),
        )
        save_project_meta(project_id)  # 失败时也持久化状态

