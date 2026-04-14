import os
import sqlite3
import secrets
from datetime import datetime
from typing import Optional
from dataclasses import dataclass
from fastapi import HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from core.config import get_config
from models.auths import User
from services.auth import hash_password, verify_password, verify_jwt_token



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
        # # 如果表存在但缺少 avatar_url 列
        # if "avatar_url" not in columns:
        #     conn.execute("ALTER TABLE users ADD COLUMN avatar_url TEXT")

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
    hashed_password = hash_password(password)
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

    new_hashed = hash_password(new_password)
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
    

    
