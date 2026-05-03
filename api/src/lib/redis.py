import redis.asyncio as aioredis
from arq.connections import ArqRedis, RedisSettings, create_pool
from src.config.env import settings

_client: aioredis.Redis | None = None
_arq_pool: ArqRedis | None = None


async def get_redis_client() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _client


async def get_arq_pool() -> ArqRedis:
    global _arq_pool
    if _arq_pool is None:
        _arq_pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
    return _arq_pool
