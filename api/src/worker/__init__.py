from arq.connections import RedisSettings
from src.config.env import settings
from src.worker.jobs import ingest_document


class WorkerSettings:
    functions = [ingest_document]
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    max_jobs = 1
    job_timeout = 600
    keep_result = 3600
    allow_abort_jobs = True
