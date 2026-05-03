from fastapi import APIRouter, Request, HTTPException

from src.config.env import settings
from src.db.base import async_session_factory
from src.db.models import Tenant
from src.auth.clerk import get_or_create_tenant

router = APIRouter()


@router.post("/webhooks/clerk", status_code=200)
async def clerk_webhook(request: Request):
    body = await request.body()

    # In demo mode just ack — no signature to verify
    if settings.AUTH_MODE == "demo":
        return {"received": True}

    from svix.webhooks import Webhook, WebhookVerificationError

    wh = Webhook(settings.CLERK_WEBHOOK_SECRET)
    try:
        event = wh.verify(body, dict(request.headers))
    except WebhookVerificationError:
        raise HTTPException(status_code=401)

    event_type = event.get("type", "")
    data = event.get("data", {})

    if event_type == "user.created":
        async with async_session_factory() as db:
            await get_or_create_tenant(data["id"], db)

    elif event_type == "user.deleted":
        from datetime import datetime, timezone
        from sqlalchemy import update

        async with async_session_factory() as db:
            await db.execute(
                update(Tenant)
                .where(Tenant.clerk_user_id == data["id"])
                .values(deleted_at=datetime.now(timezone.utc))
            )
            await db.commit()

    return {"received": True}
