import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from src.main import app


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_no_auth_returns_401(client):
    response = await client.get("/api/v1/chatbots")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.asyncio
async def test_demo_auth_creates_tenant_and_starter_chatbot(client):
    # Use a unique token so this test doesn't collide with other runs
    token = "slice2_test_user_alice"
    response = await client.get("/api/v1/chatbots", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 1
    chatbot_names = [c["name"] for c in data["items"]]
    assert "My First Chatbot" in chatbot_names


@pytest.mark.asyncio
async def test_different_tokens_get_different_tenants(client):
    token_a = "slice2_isolation_user_aaa"
    token_b = "slice2_isolation_user_bbb"

    r1 = await client.get("/api/v1/chatbots", headers={"Authorization": f"Bearer {token_a}"})
    r2 = await client.get("/api/v1/chatbots", headers={"Authorization": f"Bearer {token_b}"})

    assert r1.status_code == 200
    assert r2.status_code == 200

    tenant_a = r1.json()["items"][0]["tenant_id"]
    tenant_b = r2.json()["items"][0]["tenant_id"]
    assert tenant_a != tenant_b


@pytest.mark.asyncio
async def test_same_token_returns_same_tenant(client):
    token = "slice2_idempotent_user"

    r1 = await client.get("/api/v1/chatbots", headers={"Authorization": f"Bearer {token}"})
    r2 = await client.get("/api/v1/chatbots", headers={"Authorization": f"Bearer {token}"})

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["items"][0]["tenant_id"] == r2.json()["items"][0]["tenant_id"]


@pytest.mark.asyncio
async def test_empty_bearer_token_returns_401(client):
    # HTTPBearer will reject empty credentials before our handler runs
    response = await client.get("/api/v1/chatbots", headers={"Authorization": "Bearer "})
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_clerk_mode_invalid_jwt_returns_401(client, monkeypatch):
    import src.config.env as env_module

    monkeypatch.setattr(env_module.settings, "AUTH_MODE", "clerk")
    response = await client.get(
        "/api/v1/chatbots", headers={"Authorization": "Bearer garbage.jwt.token"}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_tenant_where_scopes_correctly(client):
    # Two users should not see each other's chatbots
    token_x = "slice2_scope_user_xxx"
    token_y = "slice2_scope_user_yyy"

    # Create both tenants
    await client.get("/api/v1/chatbots", headers={"Authorization": f"Bearer {token_x}"})
    await client.get("/api/v1/chatbots", headers={"Authorization": f"Bearer {token_y}"})

    r_x = await client.get("/api/v1/chatbots", headers={"Authorization": f"Bearer {token_x}"})
    r_y = await client.get("/api/v1/chatbots", headers={"Authorization": f"Bearer {token_y}"})

    chatbot_ids_x = {c["id"] for c in r_x.json()["items"]}
    chatbot_ids_y = {c["id"] for c in r_y.json()["items"]}
    assert chatbot_ids_x.isdisjoint(chatbot_ids_y), "Cross-tenant data leak detected"


@pytest.mark.asyncio
async def test_webhook_demo_mode_acks(client):
    response = await client.post("/webhooks/clerk", content=b"{}")
    assert response.status_code == 200
    assert response.json() == {"received": True}
