"""
芝麻开门 Open-Door
认证模块 - 用户注册、登录、JWT Token 管理

功能：
- 用户注册与登录
- JWT Token 生成与验证
- 密码哈希存储
- 用户依赖注入
"""

import os

from fastapi import Depends, HTTPException, status
from models.auths import (
    User, 
    UserCreate, 
    UserLogin, 
    UpdateProfileRequest, 
    ChangePasswordRequest, 
    UpdatePreferencesRequest, 
    TokenResponse, 
    UserResponse, 
    UserPreferencesResponse
)
# 导入配置
from core.config import get_config, PilipiliConfig
from services.auth import create_jwt_token
from services.user import (
    TokenData,
    create_user,
    authenticate_user,
    get_user_by_id,
    update_user_profile,
    change_user_password,
    get_user_preferences,
    update_user_preferences,
    get_current_user
)


# ============================================================
# API 端点
# ============================================================

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import uuid

# 创建路由
router = APIRouter(prefix="/api/auth", tags=["认证"])


@router.post("/register", response_model=TokenResponse)
async def register(user_data: UserCreate):
    """
    用户注册

    返回 JWT Token 和用户信息
    """
    config = get_config()

    # 如果认证未启用，不允许注册
    if not config.auth.enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="当前系统未启用用户注册功能"
        )

    # 创建用户
    try:
        user = create_user(user_data.username, user_data.email, user_data.password)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"注册失败: {str(e)}"
        )

    # 生成 Token
    token, expires_in = create_jwt_token(user.id, user.username, config)

    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=expires_in,
        user={
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "is_admin": user.is_admin,
        },
    )


@router.post("/login", response_model=TokenResponse)
async def login(credentials: UserLogin):
    """
    用户登录

    返回 JWT Token 和用户信息
    """
    config = get_config()

    # 验证用户
    user = authenticate_user(credentials.username, credentials.password)

    if not user:
        # 检查是否是认证未启用的情况（单用户模式）
        if not config.auth.enabled:
            # 单用户模式下，不允许登录
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="当前系统未启用登录功能"
            )

        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")

    # 生成 Token
    token, expires_in = create_jwt_token(user.id, user.username, config)

    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=expires_in,
        user={
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "is_admin": user.is_admin,
        },
    )


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: TokenData = Depends(get_current_user)):
    """
    获取当前用户信息
    """
    # 如果是默认用户（认证未启用）
    config = get_config()
    if not config.auth.enabled or current_user.user_id == config.auth.default_user_id:
        return UserResponse(
            id=config.auth.default_user_id,
            username="default_user",
            email="local@pilipili",
            avatar_url=None,
            created_at="2024-01-01T00:00:00",
            is_admin=True,  # 默认用户视为管理员
        )

    user = get_user_by_id(current_user.user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        avatar_url=None,  # 暂不返回完整 URL
        created_at=user.created_at,
        is_admin=user.is_admin,
    )


@router.post("/logout")
async def logout():
    """
    用户登出

    前端删除 Token 即可，后端无需处理
    """
    return {"message": "登出成功"}


@router.put("/profile", response_model=UserResponse)
async def update_profile(
    request: UpdateProfileRequest, current_user: TokenData = Depends(get_current_user)
):
    """
    修改用户信息（昵称、邮箱、头像）
    """
    config = get_config()
    if not config.auth.enabled:
        raise HTTPException(status_code=403, detail="当前系统未启用用户管理")

    try:
        user = update_user_profile(
            current_user.user_id,
            username=request.username,
            email=request.email,
            avatar_url=request.avatar_url,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新失败: {str(e)}")

    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        avatar_url=None,  # 暂不返回完整 URL
        created_at=user.created_at,
        is_admin=user.is_admin,
    )


@router.post("/password")
async def change_password(
    request: ChangePasswordRequest, current_user: TokenData = Depends(get_current_user)
):
    """
    修改密码
    """
    config = get_config()
    if not config.auth.enabled:
        raise HTTPException(status_code=403, detail="当前系统未启用用户管理")

    try:
        success = change_user_password(
            current_user.user_id, request.old_password, request.new_password
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"修改密码失败: {str(e)}")

    if not success:
        raise HTTPException(status_code=400, detail="原密码错误")

    return {"message": "密码修改成功"}


@router.get("/preferences", response_model=UserPreferencesResponse)
async def get_preferences(current_user: TokenData = Depends(get_current_user)):
    """
    获取用户偏好设置
    """
    prefs = get_user_preferences(current_user.user_id)
    return UserPreferencesResponse(language=prefs["language"], theme=prefs["theme"])


@router.put("/preferences", response_model=UserPreferencesResponse)
async def update_preferences(
    request: UpdatePreferencesRequest, current_user: TokenData = Depends(get_current_user)
):
    """
    更新用户偏好设置
    """
    prefs = update_user_preferences(
        current_user.user_id, language=request.language, theme=request.theme
    )
    return UserPreferencesResponse(language=prefs["language"], theme=prefs["theme"])


@router.post("/avatar")
async def upload_avatar(
    file: UploadFile = File(...), current_user: TokenData = Depends(get_current_user)
):
    """
    上传用户头像
    """
    config = get_config()
    if not config.auth.enabled:
        raise HTTPException(status_code=403, detail="当前系统未启用用户管理")

    # 验证文件类型
    allowed_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    ext = Path(file.filename).suffix.lower()
    if ext not in allowed_exts:
        raise HTTPException(status_code=400, detail=f"不支持的图片格式: {ext}")

    # 生成唯一文件名
    unique_name = f"{current_user.user_id}_{uuid.uuid4().hex[:8]}{ext}"

    # 保存目录
    avatar_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "avatars"
    )
    os.makedirs(avatar_dir, exist_ok=True)
    save_path = os.path.join(avatar_dir, unique_name)

    # 保存文件
    with open(save_path, "wb") as f:
        content = await file.read()
        f.write(content)

    # 更新用户头像
    avatar_url = f"/data/avatars/{unique_name}"
    update_user_profile(current_user.user_id, avatar_url=avatar_url)

    return {"path": avatar_url, "filename": unique_name, "message": "头像上传成功"}


@router.get("/status")
async def auth_status():
    """
    获取认证状态
    """
    config = get_config()
    return {
        "enabled": config.auth.enabled,
        "mode": "single_user" if not config.auth.enabled else "multi_user",
    }

