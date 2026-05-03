import os
from arq.connections import RedisSettings


async def noop(ctx):
    """Placeholder — real jobs registered in Slice 5."""


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(os.getenv("REDIS_URL", "redis://redis:6379"))
    functions = [noop]
