from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.env import settings
from src.db.models import Tenant, Chatbot
from src.lib.cache import SimpleCache
from src.lib.errors import UnauthenticatedError

_tenant_cache: SimpleCache[str, str] = SimpleCache(max_size=200, ttl_seconds=60)


async def verify_token(token: str) -> str:
    """
    Returns a user_id string.
    In demo mode: the token value itself is the user_id.
    In clerk mode: validates the JWT and returns the Clerk sub claim.
    """
    if settings.AUTH_MODE == "demo":
        if not token.strip():
            raise UnauthenticatedError("Token required")
        return token.strip()

    try:
        import jwt
        from src.lib.clerk_jwks import get_signing_key  # implement separately for prod

        key = await get_signing_key(token)
        payload = jwt.decode(token, key, algorithms=["RS256"])
        return payload["sub"]
    except Exception:
        raise UnauthenticatedError()


async def get_or_create_tenant(user_id: str, db: AsyncSession) -> str:
    """
    Looks up tenant by user_id. Auto-creates on first use.
    Returns tenant_id as a string.
    """
    cached = _tenant_cache.get(user_id)
    if cached:
        return cached

    result = await db.execute(
        select(Tenant.id).where(
            Tenant.clerk_user_id == user_id,
            Tenant.deleted_at.is_(None),
        )
    )
    row = result.scalar_one_or_none()

    if row is not None:
        tid = str(row)
        _tenant_cache.set(user_id, tid)
        return tid

    # First time this user_id is seen — auto-create tenant + starter chatbot
    tenant = Tenant(clerk_user_id=user_id, name=user_id[:80], plan="free")
    db.add(tenant)
    await db.flush()  # assigns tenant.id without committing

    chatbot = Chatbot(tenant_id=tenant.id, name="My First Chatbot")
    db.add(chatbot)
    await db.commit()

    tid = str(tenant.id)
    _tenant_cache.set(user_id, tid)
    return tid
