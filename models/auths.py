from pydantic import BaseModel
from dataclasses import dataclass
from typing import Optional

# ============================================================
# 数据模型
# ============================================================


@dataclass
class User:
    """用户数据模型"""

    id: str
    username: str
    email: str
    hashed_password: str
    created_at: str
    last_login: Optional[str] = None
    is_admin: bool = False  # 是否为管理员
    avatar_url: Optional[str] = None  # 头像 URL


class UserCreate(BaseModel):
    """用户注册请求"""

    username: str
    email: str
    password: str


class UserLogin(BaseModel):
    """用户登录请求"""

    username: str
    password: str


class UpdateProfileRequest(BaseModel):
    """修改用户信息请求"""

    username: Optional[str] = None
    email: Optional[str] = None
    avatar_url: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    """修改密码请求"""

    old_password: str
    new_password: str


class UpdatePreferencesRequest(BaseModel):
    """更新用户偏好设置"""

    language: Optional[str] = None
    theme: Optional[str] = None


class TokenResponse(BaseModel):
    """Token 响应"""

    access_token: str
    token_type: str = "bearer"
    expires_in: int  # 秒
    user: dict


class UserResponse(BaseModel):
    """用户信息响应（脱敏）"""

    id: str
    username: str
    email: str
    avatar_url: Optional[str] = None
    created_at: str
    is_admin: bool = False


class UserPreferencesResponse(BaseModel):
    """用户偏好设置响应"""

    language: str = "zh-CN"
    theme: str = "system"
