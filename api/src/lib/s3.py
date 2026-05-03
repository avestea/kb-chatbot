import aioboto3
from contextlib import asynccontextmanager
from src.config.env import settings


@asynccontextmanager
async def get_s3_client():
    session = aioboto3.Session()
    async with session.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT,
        region_name=settings.S3_REGION,
        aws_access_key_id=settings.S3_ACCESS_KEY_ID,
        aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
    ) as client:
        yield client
