import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from src.main import app

ALICE = "slice3_alice"
BOB = "slice3_bob"
HEADERS_ALICE = {"Authorization": f"Bearer {ALICE}"}
HEADERS_BOB = {"Authorization": f"Bearer {BOB}"}


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def alice_chatbot(client):
    resp = await client.post(
        "/api/v1/chatbots",
        json={"name": "Alice Bot", "system_prompt_override": "Be helpful."},
        headers=HEADERS_ALICE,
    )
    assert resp.status_code == 201
    return resp.json()["chatbot"]


# ---------------------------------------------------------------------------
# POST /api/v1/chatbots
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_chatbot_returns_201(client):
    resp = await client.post(
        "/api/v1/chatbots",
        json={"name": "My Bot"},
        headers=HEADERS_ALICE,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "chatbot" in data
    cb = data["chatbot"]
    assert cb["name"] == "My Bot"
    assert cb["system_prompt_override"] is None
    assert "id" in cb
    assert "tenant_id" in cb
    assert "created_at" in cb


@pytest.mark.asyncio
async def test_create_chatbot_with_system_prompt(client):
    resp = await client.post(
        "/api/v1/chatbots",
        json={"name": "Helpful Bot", "system_prompt_override": "Always be concise."},
        headers=HEADERS_ALICE,
    )
    assert resp.status_code == 201
    assert resp.json()["chatbot"]["system_prompt_override"] == "Always be concise."


@pytest.mark.asyncio
async def test_create_chatbot_name_too_long_returns_422(client):
    resp = await client.post(
        "/api/v1/chatbots",
        json={"name": "x" * 101},
        headers=HEADERS_ALICE,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_chatbot_empty_name_returns_422(client):
    resp = await client.post(
        "/api/v1/chatbots",
        json={"name": ""},
        headers=HEADERS_ALICE,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_chatbot_requires_auth(client):
    resp = await client.post("/api/v1/chatbots", json={"name": "Bot"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/v1/chatbots
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_chatbots_returns_paginated_shape(client):
    resp = await client.get("/api/v1/chatbots", headers=HEADERS_ALICE)
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert "has_more" in data
    assert isinstance(data["items"], list)


@pytest.mark.asyncio
async def test_list_chatbots_limit_too_large_returns_422(client):
    resp = await client.get("/api/v1/chatbots?limit=200", headers=HEADERS_ALICE)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_chatbots_has_more_pagination(client):
    token = "slice3_paginate_user"
    headers = {"Authorization": f"Bearer {token}"}
    # Seed 3 chatbots for this tenant
    for i in range(3):
        await client.post("/api/v1/chatbots", json={"name": f"Bot {i}"}, headers=headers)

    resp = await client.get("/api/v1/chatbots?limit=2&offset=0", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    # At least 3 (starter chatbot + 3 created), so has_more should be true with limit=2
    assert data["has_more"] is True or data["total"] <= 2


# ---------------------------------------------------------------------------
# GET /api/v1/chatbots/{id}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_chatbot_by_id(client, alice_chatbot):
    cid = alice_chatbot["id"]
    resp = await client.get(f"/api/v1/chatbots/{cid}", headers=HEADERS_ALICE)
    assert resp.status_code == 200
    assert resp.json()["chatbot"]["id"] == cid


@pytest.mark.asyncio
async def test_get_chatbot_not_found_returns_404(client):
    import uuid
    resp = await client.get(
        f"/api/v1/chatbots/{uuid.uuid4()}", headers=HEADERS_ALICE
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_tenant_isolation_get(client, alice_chatbot):
    # Bob cannot see Alice's chatbot
    cid = alice_chatbot["id"]
    resp = await client.get(f"/api/v1/chatbots/{cid}", headers=HEADERS_BOB)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PUT /api/v1/chatbots/{id}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_name_only(client, alice_chatbot):
    cid = alice_chatbot["id"]
    original_prompt = alice_chatbot["system_prompt_override"]

    resp = await client.put(
        f"/api/v1/chatbots/{cid}",
        json={"name": "Renamed Bot"},
        headers=HEADERS_ALICE,
    )
    assert resp.status_code == 200
    updated = resp.json()["chatbot"]
    assert updated["name"] == "Renamed Bot"
    assert updated["system_prompt_override"] == original_prompt


@pytest.mark.asyncio
async def test_update_system_prompt(client, alice_chatbot):
    cid = alice_chatbot["id"]
    resp = await client.put(
        f"/api/v1/chatbots/{cid}",
        json={"system_prompt_override": "New prompt"},
        headers=HEADERS_ALICE,
    )
    assert resp.status_code == 200
    assert resp.json()["chatbot"]["system_prompt_override"] == "New prompt"


@pytest.mark.asyncio
async def test_tenant_isolation_put(client, alice_chatbot):
    cid = alice_chatbot["id"]
    resp = await client.put(
        f"/api/v1/chatbots/{cid}",
        json={"name": "Hijacked"},
        headers=HEADERS_BOB,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/v1/chatbots/{id}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_chatbot_returns_204(client, alice_chatbot):
    cid = alice_chatbot["id"]
    resp = await client.delete(f"/api/v1/chatbots/{cid}", headers=HEADERS_ALICE)
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_deleted_chatbot_disappears_from_list(client):
    token = "slice3_delete_list_user"
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post(
        "/api/v1/chatbots", json={"name": "Temp Bot"}, headers=headers
    )
    cid = create_resp.json()["chatbot"]["id"]

    await client.delete(f"/api/v1/chatbots/{cid}", headers=headers)

    list_resp = await client.get("/api/v1/chatbots", headers=headers)
    ids = [c["id"] for c in list_resp.json()["items"]]
    assert cid not in ids


@pytest.mark.asyncio
async def test_deleted_chatbot_get_returns_404(client):
    token = "slice3_delete_get_user"
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post(
        "/api/v1/chatbots", json={"name": "Gone Bot"}, headers=headers
    )
    cid = create_resp.json()["chatbot"]["id"]
    await client.delete(f"/api/v1/chatbots/{cid}", headers=headers)

    resp = await client.get(f"/api/v1/chatbots/{cid}", headers=headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_tenant_isolation_delete(client, alice_chatbot):
    cid = alice_chatbot["id"]
    resp = await client.delete(f"/api/v1/chatbots/{cid}", headers=HEADERS_BOB)
    assert resp.status_code == 404
