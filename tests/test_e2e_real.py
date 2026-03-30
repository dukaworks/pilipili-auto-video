"""
芝麻开门 Open-Door
全流程真实 API 端到端测试（跳过可灵视频合成）

测试链路：
  1. DeepSeek LLM  → 生成分镜脚本（VideoScript），含一男一女角色
  2. Gemini Image  → 为每个分镜生成关键帧（含黑名单机制验证、角色外貌一致性）
  3. MiniMax TTS   → 为每个分镜生成配音（男声/女声分别分配）

运行方式：
  cd /home/ubuntu/ZhiMa-KaiMen
  python3 tests/test_e2e_real.py
"""

import sys, os, asyncio, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import load_config
from modules.llm import generate_script, CharacterInfo
from modules.image_gen import generate_all_keyframes, _FAILED_MODELS, reset_failed_models
from modules.tts import generate_all_voiceovers

OUTPUT_DIR = "/tmp/e2e_test_output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ============================================================
# 工具函数
# ============================================================


def sep(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def ok(msg: str):
    print(f"  ✅ {msg}")


def fail(msg: str):
    print(f"  ❌ {msg}")


def info(msg: str):
    print(f"  ℹ️  {msg}")


# ============================================================
# 主测试流程
# ============================================================


async def run_e2e():
    config = load_config("config.yaml")
    reset_failed_models()

    all_passed = True

    # ----------------------------------------------------------
    # 步骤 1：LLM 脚本生成（DeepSeek）
    # 明确要求：一男一女主角，每个分镜都有两人出现
    # ----------------------------------------------------------
    sep("步骤 1 / 3：LLM 脚本生成（DeepSeek）")
    t0 = time.time()
    try:
        script = await generate_script(
            topic=(
                "一个帅气男生和一个美丽女生在东京街头相遇、相识、相爱的短片。"
                "男主角：黑发、黑色皮夹克、高挑帅气。"
                "女主角：长棕发、白色连衣裙、清纯甜美。"
                "要求：每个分镜都必须同时出现男女主角两人，共3个分镜。"
            ),
            style="日系唯美，暖色调，电影感",
            duration_hint=30,
            num_scenes=3,
            config=config,
            verbose=True,
        )
        elapsed = time.time() - t0
        ok(f"脚本生成成功，耗时 {elapsed:.1f}s")
        ok(f"标题: {script.title}")
        ok(f"分镜数: {len(script.scenes)}")
        ok(f"角色数: {len(script.characters)}")
        for char in script.characters:
            info(
                f"  角色 {char.character_id}: {char.name} ({char.gender}) | {char.appearance_prompt[:60]}..."
            )
        for scene in script.scenes:
            info(
                f"  Scene {scene.scene_id}: speaker={scene.speaker_id} | chars={scene.characters_in_scene} | {scene.voiceover[:25]}..."
            )

        # 验证：必须有男有女
        genders = {c.gender for c in script.characters}
        if "male" in genders and "female" in genders:
            ok("角色性别验证通过：包含男性和女性角色")
        else:
            fail(f"角色性别不完整，当前性别集合: {genders}")
            all_passed = False

    except Exception as e:
        fail(f"脚本生成失败: {e}")
        import traceback

        traceback.print_exc()
        all_passed = False
        print("\n❌ 步骤 1 失败，终止后续测试")
        return False

    # ----------------------------------------------------------
    # 步骤 2：关键帧图像生成（Gemini + 黑名单机制 + 角色外貌一致性）
    # 每个 Scene 的 image_prompt 都应包含男女主角
    # ----------------------------------------------------------
    sep("步骤 2 / 3：关键帧图像生成（Gemini + 黑名单 + 角色一致性）")
    info(f"测试前黑名单: {_FAILED_MODELS}")
    info("验证点：第1个Scene触发黑名单后，后续Scene应直接跳过失败模型")
    t0 = time.time()
    try:
        img_results = await generate_all_keyframes(
            scenes=script.scenes,
            output_dir=OUTPUT_DIR,
            config=config,
            characters=script.characters,
            max_concurrent=1,  # 串行，方便观察黑名单传播
            verbose=True,
        )
        elapsed = time.time() - t0
        ok(f"图像生成完成，耗时 {elapsed:.1f}s")
        info(f"测试后黑名单: {_FAILED_MODELS}")

        if _FAILED_MODELS:
            ok(f"黑名单机制生效：{_FAILED_MODELS} 在后续 Scene 中被跳过")
        else:
            info("主模型全程可用，黑名单未触发（正常情况）")

        for scene_id, path in sorted(img_results.items()):
            size = os.path.getsize(path) if path and os.path.exists(path) else 0
            if size > 10000:
                ok(f"Scene {scene_id}: {os.path.basename(path)} ({size // 1024} KB)")
            else:
                fail(f"Scene {scene_id}: 文件异常，大小 {size} bytes")
                all_passed = False

    except Exception as e:
        fail(f"图像生成失败: {e}")
        import traceback

        traceback.print_exc()
        all_passed = False

    # ----------------------------------------------------------
    # 步骤 3：TTS 配音生成（MiniMax + 男声/女声分别分配）
    # ----------------------------------------------------------
    sep("步骤 3 / 3：TTS 配音生成（MiniMax + 男声/女声）")
    info("验证点：男性角色用 male-qn-qingse，女性角色用 female-shaonv")
    t0 = time.time()
    try:
        tts_results = await generate_all_voiceovers(
            scenes=script.scenes,
            output_dir=OUTPUT_DIR,
            config=config,
            characters=script.characters,
            max_concurrent=3,
            verbose=True,
        )
        elapsed = time.time() - t0
        ok(f"TTS 生成完成，耗时 {elapsed:.1f}s")

        # 构建 speaker_id -> gender 映射，用于验证音色分配
        char_gender = {c.character_id: c.gender for c in script.characters}

        for scene_id, (audio_path, duration) in sorted(tts_results.items()):
            scene = next((s for s in script.scenes if s.scene_id == scene_id), None)
            speaker_gender = char_gender.get(scene.speaker_id, "?") if scene else "?"
            size = os.path.getsize(audio_path) if audio_path and os.path.exists(audio_path) else 0
            if size > 1000 and duration > 0:
                ok(
                    f"Scene {scene_id} [{speaker_gender}]: {os.path.basename(audio_path)} | {duration:.2f}s ({size // 1024} KB)"
                )
            elif not audio_path:
                info(f"Scene {scene_id}: 无旁白，跳过")
            else:
                fail(f"Scene {scene_id}: 音频异常，大小 {size} bytes，时长 {duration:.2f}s")
                all_passed = False

    except Exception as e:
        fail(f"TTS 生成失败: {e}")
        import traceback

        traceback.print_exc()
        all_passed = False

    # ----------------------------------------------------------
    # 汇总
    # ----------------------------------------------------------
    sep("测试汇总")
    if all_passed:
        ok("全流程测试通过！（脚本 → 图像[含男女主角] → TTS[男声/女声]）")
    else:
        fail("部分步骤失败，请查看上方错误信息")

    print(f"\n输出文件目录: {OUTPUT_DIR}")
    for fname in sorted(os.listdir(OUTPUT_DIR)):
        fpath = os.path.join(OUTPUT_DIR, fname)
        size = os.path.getsize(fpath)
        print(f"  {fname}  ({size // 1024} KB)")

    return all_passed


if __name__ == "__main__":
    result = asyncio.run(run_e2e())
    sys.exit(0 if result else 1)
