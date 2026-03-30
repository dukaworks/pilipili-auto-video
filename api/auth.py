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
import sqlite3
import json
import secrets
import hashlib
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

# 导入配置
from core.config import get_config, PilipiliConfig


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


# ============================================================
# 密码哈希（使用 PBKDF2 + SHA256）
# ============================================================


def _hash_password(password: str, salt: Optional[str] = None) -> str:
    """密码哈希 - PBKDF2 + SHA256"""
    if salt is None:
        salt = secrets.token_hex(32)

    # PBKDF2-HMAC-SHA256
    key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        100000,  # 迭代次数
    )
    return f"{salt}${key.hex()}"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码"""
    try:
        salt, key_hash = hashed_password.split("$")
        computed_key = hashlib.pbkdf2_hmac(
            "sha256", plain_password.encode("utf-8"), salt.encode("utf-8"), 100000
        )
        return computed_key.hex() == key_hash
    except Exception:
        return False


# ============================================================
# JWT Token 管理
# ============================================================


def _base64_url_encode(data: bytes) -> str:
    """Base64 URL 编码"""
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def _base64_url_decode(data: str) -> bytes:
    """Base64 URL 解码"""
    import base64

    padding = 4 - (len(data) % 4)
    if padding != 4:
        data += "=" * padding
    return base64.urlsafe_b64decode(data)


def create_jwt_token(user_id: str, username: str, config: PilipiliConfig) -> tuple[str, int]:
    """创建 JWT Token

    返回: (token, expires_in_seconds)
    """
    if not config.auth.jwt_secret:
        # 自动生成密钥（首次启动时）
        config.auth.jwt_secret = secrets.token_urlsafe(32)

    # Header
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = _base64_url_encode(json.dumps(header).encode())

    # Payload
    now = datetime.now()
    exp = now + timedelta(hours=config.auth.jwt_expire_hours)
    payload = {
        "sub": user_id,
        "name": username,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    payload_b64 = _base64_url_encode(json.dumps(payload).encode())

    # Signature
    message = f"{header_b64}.{payload_b64}"
    signature = hmac_sha256(config.auth.jwt_secret, message)
    signature_b64 = _base64_url_encode(signature)

    token = f"{header_b64}.{payload_b64}.{signature_b64}"
    expires_in = config.auth.jwt_expire_hours * 3600

    return token, expires_in


def hmac_sha256(key: str, message: str) -> bytes:
    """HMAC-SHA256"""
    import hmac

    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()


def verify_jwt_token(token: str, config: PilipiliConfig) -> Optional[dict]:
    """验证 JWT Token

    返回: payload dict 或 None（如果无效）
    """
    if not config.auth.jwt_secret:
        return None

    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None

        header_b64, payload_b64, signature_b64 = parts

        # 验证签名
        message = f"{header_b64}.{payload_b64}"
        expected_signature = hmac_sha256(config.auth.jwt_secret, message)
        expected_signature_b64 = _base64_url_encode(expected_signature)

        if signature_b64 != expected_signature_b64:
            return None

        # 解析 payload
        payload = json.loads(_base64_url_decode(payload_b64))

        # 检查过期
        if payload.get("exp", 0) < datetime.now().timestamp():
            return None

        return payload

    except Exception:
        return None


# ============================================================
# 用户数据库操作
# ============================================================


def _get_auth_db_path() -> str:
    """获取认证数据库路径"""
    config = get_config()
    db_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "auth"
    )
    os.makedirs(db_dir, exist_ok=True)
    return os.path.join(db_dir, "users.db")


def init_auth_db():
    """初始化认证数据库"""
    db_path = _get_auth_db_path()
    with sqlite3.connect(db_path) as conn:
        # 获取现有表结构
        cursor = conn.execute("PRAGMA table_info(users)")
        columns = {row[1] for row in cursor.fetchall()}

        # 如果表不存在，创建它
        if not columns:
            conn.execute("""
                CREATE TABLE users (
                    id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    email TEXT UNIQUE NOT NULL,
                    hashed_password TEXT NOT NULL,
                    avatar_url TEXT,
                    created_at TEXT NOT NULL,
                    last_login TEXT,
                    is_admin INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE INDEX idx_users_username ON users(username)
            """)
        # 如果表存在但缺少 is_admin 列
        elif "is_admin" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
        # 如果表存在但缺少 avatar_url 列
        if "avatar_url" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN avatar_url TEXT")

        # 创建用户偏好设置表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id TEXT PRIMARY KEY,
                language TEXT DEFAULT 'zh-CN',
                theme TEXT DEFAULT 'system',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # 确保索引存在
        try:
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)
            """)
        except sqlite3.OperationalError:
            pass  # 索引已存在


def create_user(username: str, email: str, password: str) -> User:
    """创建新用户"""
    db_path = _get_auth_db_path()

    # 检查用户名和邮箱是否已存在
    with sqlite3.connect(db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM users WHERE username = ? OR email = ?", (username, email)
        ).fetchone()

        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="用户名或邮箱已存在"
            )

        # 检查是否是第一个用户（管理员）
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        is_admin = count == 0  # 第一个用户为管理员

    # 创建用户
    user_id = secrets.token_urlsafe(16)
    hashed_password = _hash_password(password)
    now = datetime.now().isoformat()

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO users (id, username, email, hashed_password, created_at, is_admin)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, username, email, hashed_password, now, 1 if is_admin else 0),
        )

    return User(
        id=user_id,
        username=username,
        email=email,
        hashed_password=hashed_password,
        created_at=now,
        is_admin=is_admin,
    )


def authenticate_user(username: str, password: str) -> Optional[User]:
    """验证用户登录"""
    db_path = _get_auth_db_path()

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, username, email, hashed_password, created_at, last_login, is_admin FROM users WHERE username = ?",
            (username,),
        ).fetchone()

    if not row:
        return None

    if not verify_password(password, row[3]):
        return None

    # 更新最后登录时间
    now = datetime.now().isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (now, row[0]))

    is_admin = row[6] == 1 if len(row) > 6 else False
    return User(
        id=row[0],
        username=row[1],
        email=row[2],
        hashed_password=row[3],
        created_at=row[4],
        last_login=now,
        is_admin=is_admin,
    )


def get_user_by_id(user_id: str) -> Optional[User]:
    """根据 ID 获取用户"""
    db_path = _get_auth_db_path()

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, username, email, hashed_password, created_at, last_login, is_admin, avatar_url FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()

    if not row:
        return None

    is_admin = row[6] == 1 if len(row) > 6 else False
    avatar_url = row[7] if len(row) > 7 else None
    return User(
        id=row[0],
        username=row[1],
        email=row[2],
        hashed_password=row[3],
        created_at=row[4],
        last_login=row[5],
        is_admin=is_admin,
        avatar_url=avatar_url,
    )


def update_user_profile(
    user_id: str,
    username: Optional[str] = None,
    email: Optional[str] = None,
    avatar_url: Optional[str] = None,
) -> Optional[User]:
    """更新用户信息"""
    db_path = _get_auth_db_path()

    # 检查用户名/邮箱是否已被占用
    if username or email:
        with sqlite3.connect(db_path) as conn:
            query_parts = []
            params = []
            if username:
                query_parts.append("username = ?")
                params.append(username)
            if email:
                query_parts.append("email = ?")
                params.append(email)
            params.append(user_id)

            # 检查是否被其他用户占用
            existing = conn.execute(
                f"SELECT id FROM users WHERE ({','.join(query_parts)}) AND id != ?", params
            ).fetchone()

            if existing:
                raise HTTPException(status_code=400, detail="用户名或邮箱已被占用")

    # 更新用户信息
    updates = []
    params = []
    if username:
        updates.append("username = ?")
        params.append(username)
    if email:
        updates.append("email = ?")
        params.append(email)
    if avatar_url is not None:
        updates.append("avatar_url = ?")
        params.append(avatar_url)

    if not updates:
        return get_user_by_id(user_id)

    params.append(user_id)
    with sqlite3.connect(db_path) as conn:
        conn.execute(f"UPDATE users SET {','.join(updates)} WHERE id = ?", params)

    return get_user_by_id(user_id)


def change_user_password(user_id: str, old_password: str, new_password: str) -> bool:
    """修改用户密码"""
    user = get_user_by_id(user_id)
    if not user:
        return False

    if not verify_password(old_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="原密码错误")

    new_hashed = _hash_password(new_password)
    db_path = _get_auth_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE users SET hashed_password = ? WHERE id = ?", (new_hashed, user_id))
    return True


def get_user_preferences(user_id: str) -> dict:
    """获取用户偏好设置"""
    db_path = _get_auth_db_path()
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT language, theme FROM user_preferences WHERE user_id = ?",
            (user_id,),
        ).fetchone()

    if not row:
        # 返回默认值
        return {"language": "zh-CN", "theme": "system"}

    return {"language": row[0], "theme": row[1]}


def update_user_preferences(
    user_id: str, language: Optional[str] = None, theme: Optional[str] = None
) -> dict:
    """更新用户偏好设置"""
    db_path = _get_auth_db_path()
    now = datetime.now().isoformat()

    with sqlite3.connect(db_path) as conn:
        # 检查是否已存在
        existing = conn.execute(
            "SELECT user_id FROM user_preferences WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if existing:
            # 更新
            updates = []
            params = []
            if language:
                updates.append("language = ?")
                params.append(language)
            if theme:
                updates.append("theme = ?")
                params.append(theme)
            updates.append("updated_at = ?")
            params.append(now)
            params.append(user_id)

            if updates:
                conn.execute(
                    f"UPDATE user_preferences SET {','.join(updates)} WHERE user_id = ?", params
                )
        else:
            # 插入
            language = language or "zh-CN"
            theme = theme or "system"
            conn.execute(
                "INSERT INTO user_preferences (user_id, language, theme, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, language, theme, now, now),
            )

    return get_user_preferences(user_id)


# ============================================================
# FastAPI 依赖注入
# ============================================================

security = HTTPBearer()


@dataclass
class TokenData:
    """Token 数据"""

    user_id: str
    username: str


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> TokenData:
    """获取当前登录用户（需要 Token）"""
    config = get_config()

    # 如果认证未启用，返回默认用户
    if not config.auth.enabled:
        return TokenData(user_id=config.auth.default_user_id, username="default_user")

    token = credentials.credentials
    payload = verify_jwt_token(token, config)

    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的 Token 或 Token 已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return TokenData(user_id=payload["sub"], username=payload["name"])


async def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False)),
) -> TokenData:
    """获取当前用户（可选，不强制）"""
    config = get_config()

    # 如果认证未启用，返回默认用户
    if not config.auth.enabled:
        return TokenData(user_id=config.auth.default_user_id, username="default_user")

    if not credentials:
        return TokenData(user_id=config.auth.default_user_id, username="default_user")

    token = credentials.credentials
    payload = verify_jwt_token(token, config)

    if not payload:
        return TokenData(user_id=config.auth.default_user_id, username="default_user")

    return TokenData(user_id=payload["sub"], username=payload["name"])


# ============================================================
# 初始化
# ============================================================


def init_auth():
    """初始化认证模块"""
    init_auth_db()


# ============================================================
# API 端点
# ============================================================

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

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


# 启动时初始化
init_auth()
