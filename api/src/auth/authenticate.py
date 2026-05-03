from fastapi import Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.auth.clerk import verify_token, get_or_create_tenant
from src.lib.errors import UnauthenticatedError

security = HTTPBearer(auto_error=False)


class AuthenticatedTenant:
    def __init__(self, tenant_id: str, user_id: str):
        self.tenant_id = tenant_id
        self.user_id = user_id


async def get_current_tenant(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> AuthenticatedTenant:
    if credentials is None:
        raise UnauthenticatedError()

    user_id = await verify_token(credentials.credentials)
    tenant_id = await get_or_create_tenant(user_id, db)
    return AuthenticatedTenant(tenant_id=tenant_id, user_id=user_id)
