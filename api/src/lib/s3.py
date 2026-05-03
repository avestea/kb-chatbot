import aioboto3
from src.config.env import settings

_session = aioboto3.Session(
    aws_access_key_id=settings.S3_ACCESS_KEY_ID,
    aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
    region_name=settings.S3_REGION,
)


def s3_client():
    """Use as async context manager: async with s3_client() as s3: ..."""
    kwargs = {}
    if settings.S3_ENDPOINT:
        kwargs["endpoint_url"] = settings.S3_ENDPOINT
    return _session.client("s3", **kwargs)
