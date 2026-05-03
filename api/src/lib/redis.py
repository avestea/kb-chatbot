import redis.asyncio as redis
from src.config.env import settings

_client: redis.Redis | None = None


async def get_redis_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _client
