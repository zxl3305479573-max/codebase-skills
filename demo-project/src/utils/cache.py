"""
Cache utility — 缓存工具

模拟 Redis 客户端（实际项目使用 redis-py）
"""


class _MockRedis:
    """模拟 Redis 客户端 — 用于本地测试"""

    def __init__(self):
        self._store: dict[str, tuple[str, int | None]] = {}

    def get(self, key: str) -> str | None:
        """获取 key"""
        import time
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expire_at = entry
        if expire_at and time.time() > expire_at:
            del self._store[key]
            return None
        return value

    def setex(self, key: str, ttl: int, value: str) -> None:
        """设置 key 带过期时间"""
        import time
        self._store[key] = (value, time.time() + ttl)

    def delete(self, key: str) -> None:
        """删除 key"""
        self._store.pop(key, None)

    def incr(self, key: str) -> int:
        """自增"""
        val = int(self.get(key) or 0) + 1
        self._store[key] = (str(val), None)
        return val


redis_client = _MockRedis()
