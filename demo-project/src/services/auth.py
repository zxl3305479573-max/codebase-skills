"""
Auth service — 认证模块

用户注册、登录、Token 管理、权限校验。
"""

import hashlib
import logging
import time
from datetime import datetime, timedelta

from src.utils.db import get_db
from src.utils.cache import redis_client

logger = logging.getLogger(__name__)

TOKEN_EXPIRE_HOURS = 24
REFRESH_TOKEN_EXPIRE_DAYS = 30


class AuthService:
    """用户认证服务"""

    def register(self, username: str, password: str,
                 email: str, phone: str = "") -> dict:
        """用户注册"""
        # 检查用户名唯一性
        if self._username_exists(username):
            raise ValueError(f"Username already taken: {username}")

        # 密码哈希
        password_hash = self._hash_password(password)

        # 写入数据库
        db = get_db()
        user_id = db.insert(
            """INSERT INTO users (username, password_hash, email, phone, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (username, password_hash, email, phone, datetime.now()),
        )

        logger.info(f"User registered: {username}, id={user_id}")
        return {"user_id": user_id, "username": username}

    def login(self, username: str, password: str) -> dict:
        """用户登录 — 返回 access_token"""
        db = get_db()
        user = db.query(
            "SELECT * FROM users WHERE username = ?", (username,)
        )

        if not user:
            raise ValueError("Invalid username or password")

        # 验证密码
        password_hash = self._hash_password(password)
        if password_hash != user["password_hash"]:
            logger.warning(f"Login failed for {username}: wrong password")
            raise ValueError("Invalid username or password")

        # 生成 Token
        access_token = self._generate_token(user["id"])
        refresh_token = self._generate_refresh_token(user["id"])

        # 缓存 Token
        redis_client.setex(
            f"token:{access_token}",
            TOKEN_EXPIRE_HOURS * 3600,
            str(user["id"]),
        )

        logger.info(f"User logged in: {username}")
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": TOKEN_EXPIRE_HOURS * 3600,
            "user_id": user["id"],
        }

    def verify_token(self, token: str) -> str | None:
        """验证 access token，返回 user_id 或 None"""
        user_id = redis_client.get(f"token:{token}")
        if user_id:
            return user_id

        # 尝试解析 JWT（兼容旧版）
        return self._verify_jwt_token(token)

    def refresh_access_token(self, refresh_token: str) -> dict:
        """使用 refresh token 换新的 access token"""
        user_id = redis_client.get(f"refresh:{refresh_token}")
        if not user_id:
            raise ValueError("Invalid or expired refresh token")

        # 生成新 token
        new_access = self._generate_token(user_id)
        redis_client.setex(
            f"token:{new_access}",
            TOKEN_EXPIRE_HOURS * 3600,
            user_id,
        )
        return {"access_token": new_access, "expires_in": TOKEN_EXPIRE_HOURS * 3600}

    def logout(self, token: str) -> bool:
        """登出 — 清除 token"""
        user_id = redis_client.get(f"token:{token}")
        if user_id:
            redis_client.delete(f"token:{token}")
            logger.info(f"User logged out: {user_id}")
            return True
        return False

    def check_permission(self, user_id: str, resource: str,
                         action: str) -> bool:
        """检查用户权限"""
        db = get_db()
        role = db.query(
            """SELECT r.permissions FROM roles r
               JOIN user_roles ur ON r.id = ur.role_id
               WHERE ur.user_id = ?""",
            (user_id,),
        )
        if not role:
            return False
        required = f"{resource}:{action}"
        return required in role["permissions"]

    def _username_exists(self, username: str) -> bool:
        db = get_db()
        row = db.query(
            "SELECT id FROM users WHERE username = ?", (username,)
        )
        return row is not None

    @staticmethod
    def _hash_password(password: str) -> str:
        return hashlib.sha256(
            f"{password}_salt_v2".encode()
        ).hexdigest()

    @staticmethod
    def _generate_token(user_id: str) -> str:
        import uuid
        payload = f"{user_id}:{time.time()}:{uuid.uuid4().hex}"
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    def _generate_refresh_token(user_id: str) -> str:
        import uuid
        return f"rt_{user_id}_{uuid.uuid4().hex}"

    @staticmethod
    def _verify_jwt_token(token: str) -> str | None:
        # 兼容旧的 JWT 格式
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return None
            import json, base64
            payload = json.loads(base64.b64decode(parts[1] + "=="))
            if payload.get("exp", 0) > time.time():
                return payload.get("sub")
        except Exception:
            pass
        return None
