# Slice 2 — Auth & Multi-Tenancy

**Depends on:** Slice 1
**Prereq:** Read `00-prompt-prefix.md` first.

---

## Goal

Every request is scoped to a tenant. The system has two modes controlled by a single env var:

| `AUTH_MODE` | Behaviour |
|---|---|
| `demo` (default) | Any Bearer token works. The token value is the user identity. Tenant is auto-created on first use. No Clerk account needed. |
| `clerk` | Bearer token must be a valid Clerk JWT. `CLERK_SECRET_KEY` required. |

Switch from demo to real Clerk by changing one env var — no code changes.

## Deliverables

- `api/src/auth/clerk.py` — token verification + tenant auto-create.
- `api/src/auth/authenticate.py` — FastAPI `Depends` that attaches `tenant_id`.
- `api/src/auth/webhook.py` — `POST /webhooks/clerk` (only used in `AUTH_MODE=clerk`).
- `api/src/db/tenant_scope.py` — `tenant_where()` SQL helper (locked cross-slice contract).
- `api/src/lib/cache.py` — TTL in-memory cache.

---

## Tenant-scope helper (`api/src/db/tenant_scope.py`)

Locked contract. Implement exactly:

```python
from sqlalchemy import and_, ColumnElement

def tenant_where(model, tenant_id: str, additional: ColumnElement | None = None) -> ColumnElement:
    """
    Composes: model.tenant_id == tenant_id
              AND model.deleted_at IS NULL  (if column exists)
              AND additional?
    Every read query against a soft-deletable table MUST use this.
    Never write raw model.tenant_id == ... directly.
    """
    conditions = [model.tenant_id == tenant_id]
    if hasattr(model, "deleted_at"):
        conditions.append(model.deleted_at.is_(None))
    if additional is not None:
        conditions.append(additional)
    return and_(*conditions)
```

---

## Auth core (`api/src/auth/clerk.py`)

```python
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

    # ── Clerk JWT verification ──────────────────────────────────────────
    # Requires: pip install PyJWT cryptography httpx
    # Clerk exposes its JWKS at https://api.clerk.com/v1/jwks
    # Cache the JWKS in Redis or a module-level dict to avoid re-fetching.
    try:
        import jwt
        from src.lib.clerk_jwks import get_signing_key  # implement separately

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
```

---

## FastAPI dependency (`api/src/auth/authenticate.py`)

```python
from fastapi import Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.auth.clerk import verify_token, get_or_create_tenant

security = HTTPBearer(auto_error=False)


class AuthenticatedTenant:
    def __init__(self, tenant_id: str, user_id: str):
        self.tenant_id = tenant_id
        self.user_id = user_id


async def get_current_tenant(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> AuthenticatedTenant:
    from src.lib.errors import UnauthenticatedError

    if credentials is None:
        raise UnauthenticatedError()

    user_id = await verify_token(credentials.credentials)
    tenant_id = await get_or_create_tenant(user_id, db)
    return AuthenticatedTenant(tenant_id=tenant_id, user_id=user_id)
```

Usage in any route:

```python
from src.auth.authenticate import get_current_tenant, AuthenticatedTenant
from fastapi import Depends

@router.get("/chatbots")
async def list_chatbots(auth: AuthenticatedTenant = Depends(get_current_tenant), ...):
    # auth.tenant_id is always set and valid
    ...
```

---

## Clerk webhook (`api/src/auth/webhook.py`)

Only relevant in `AUTH_MODE=clerk`. In demo mode this endpoint still exists but does nothing because tenants are auto-created by `get_or_create_tenant`.

```python
from fastapi import APIRouter, Request, HTTPException
from svix.webhooks import Webhook, WebhookVerificationError

from src.config.env import settings
from src.db.base import async_session
from src.db.models import Tenant
from src.auth.clerk import get_or_create_tenant

router = APIRouter()


@router.post("/webhooks/clerk", status_code=200)
async def clerk_webhook(request: Request):
    body = await request.body()

    # In demo mode just ack — no signature to verify
    if settings.AUTH_MODE == "demo":
        return {"received": True}

    wh = Webhook(settings.CLERK_WEBHOOK_SECRET)
    try:
        event = wh.verify(body, dict(request.headers))
    except WebhookVerificationError:
        raise HTTPException(status_code=401)

    event_type = event.get("type", "")
    data = event.get("data", {})

    if event_type == "user.created":
        async with async_session() as db:
            await get_or_create_tenant(data["id"], db)

    elif event_type == "user.deleted":
        from datetime import datetime, timezone
        from sqlalchemy import update

        async with async_session() as db:
            await db.execute(
                update(Tenant)
                .where(Tenant.clerk_user_id == data["id"])
                .values(deleted_at=datetime.now(timezone.utc))
            )
            await db.commit()

    return {"received": True}
```

---

## In-memory cache (`api/src/lib/cache.py`)

```python
import time
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class SimpleCache(Generic[K, V]):
    def __init__(self, max_size: int = 100, ttl_seconds: int = 60):
        self._store: dict[K, tuple[V, float]] = {}
        self.max_size = max_size
        self.ttl = ttl_seconds

    def get(self, key: K) -> V | None:
        entry = self._store.get(key)
        if entry and time.time() - entry[1] < self.ttl:
            return entry[0]
        self._store.pop(key, None)
        return None

    def set(self, key: K, value: V) -> None:
        if len(self._store) >= self.max_size:
            oldest = min(self._store, key=lambda k: self._store[k][1])
            del self._store[oldest]
        self._store[key] = (value, time.time())
```

---

## Wire into `main.py`

```python
from src.auth.webhook import router as webhook_router
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webhook_router)

# All /api/v1/* routes apply get_current_tenant via Depends in each router
```

---

## Add to `Settings` (`api/src/config/env.py`)

```python
AUTH_MODE: str = "demo"          # "demo" | "clerk"
CLERK_SECRET_KEY: str = ""       # only required when AUTH_MODE=clerk
CLERK_WEBHOOK_SECRET: str = ""   # only required when AUTH_MODE=clerk
```

---

## Demo usage

```bash
# Any string works as a token in demo mode.
# First request auto-creates the tenant.

curl localhost:8000/api/v1/chatbots \
  -H "Authorization: Bearer alice"

# Or from the Gradio UI: enter "alice" in the token field.
# A second user "bob" gets a completely separate tenant.
```

---

## Switching to real Clerk (when ready)

1. Create a Clerk application at clerk.com.
2. Copy the Secret Key and Webhook Secret.
3. Set in `.env`:
   ```
   AUTH_MODE=clerk
   CLERK_SECRET_KEY=sk_live_...
   CLERK_WEBHOOK_SECRET=whsec_...
   ```
4. Implement `api/src/lib/clerk_jwks.py` — fetch JWKS from Clerk and verify the JWT (one ~30-line file). The rest of the codebase is unchanged.
5. Point the Clerk dashboard webhook at `POST https://yourdomain.com/webhooks/clerk`.

---

## Acceptance criteria

- `AUTH_MODE=demo`: `GET /api/v1/chatbots -H "Authorization: Bearer alice"` returns 200 and a starter chatbot exists.
- Same request with token `"bob"` returns a different tenant's data.
- Request with no `Authorization` header returns 401.
- `tenant_where()` used in every query — cross-tenant reads return 404.
- `AUTH_MODE=clerk` with an invalid JWT returns 401 (manual test with a garbage token).
