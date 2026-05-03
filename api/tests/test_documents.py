import uuid
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, MagicMock, patch

from src.main import app

ALICE = "slice4_alice"
BOB = "slice4_bob"
HEADERS_ALICE = {"Authorization": f"Bearer {ALICE}"}
HEADERS_BOB = {"Authorization": f"Bearer {BOB}"}

TINY_PDF = b"%PDF-1.4\n%EOF\n"
TINY_TXT = b"Hello, world!"
TINY_HTML = b"<html><body>Test</body></html>"


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def alice_chatbot(client):
    resp = await client.post(
        "/api/v1/chatbots",
        json={"name": "Alice Doc Bot"},
        headers=HEADERS_ALICE,
    )
    assert resp.status_code == 201
    return resp.json()["chatbot"]


@pytest.fixture
def mock_s3():
    s3_mock = AsyncMock()
    s3_mock.put_object = AsyncMock()
    s3_mock.delete_object = AsyncMock()

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=s3_mock)
    cm.__aexit__ = AsyncMock(return_value=False)

    with patch("src.routes.documents.s3_client", return_value=cm):
        yield s3_mock


@pytest.fixture
def mock_arq():
    arq_mock = AsyncMock()
    arq_mock.enqueue_job = AsyncMock()

    with patch("src.lib.redis.get_arq_pool", new=AsyncMock(return_value=arq_mock)):
        yield arq_mock


# ---------------------------------------------------------------------------
# POST /api/v1/chatbots/{chatbot_id}/documents
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upload_pdf_returns_201(client, alice_chatbot, mock_s3, mock_arq):
    cid = alice_chatbot["id"]
    resp = await client.post(
        f"/api/v1/chatbots/{cid}/documents",
        files={"file": ("test.pdf", TINY_PDF, "application/pdf")},
        headers=HEADERS_ALICE,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "document" in data
    doc = data["document"]
    assert doc["filename"] == "test.pdf"
    assert doc["mime_type"] == "application/pdf"
    assert doc["status"] == "pending"
    assert doc["chatbot_id"] == cid
    assert "id" in doc
    assert "created_at" in doc


@pytest.mark.asyncio
async def test_upload_txt_returns_201(client, alice_chatbot, mock_s3, mock_arq):
    cid = alice_chatbot["id"]
    resp = await client.post(
        f"/api/v1/chatbots/{cid}/documents",
        files={"file": ("notes.txt", TINY_TXT, "text/plain")},
        headers=HEADERS_ALICE,
    )
    assert resp.status_code == 201
    assert resp.json()["document"]["mime_type"] == "text/plain"


@pytest.mark.asyncio
async def test_upload_stores_in_s3(client, alice_chatbot, mock_s3, mock_arq):
    cid = alice_chatbot["id"]
    await client.post(
        f"/api/v1/chatbots/{cid}/documents",
        files={"file": ("report.pdf", TINY_PDF, "application/pdf")},
        headers=HEADERS_ALICE,
    )
    mock_s3.put_object.assert_called_once()
    call_kwargs = mock_s3.put_object.call_args.kwargs
    assert call_kwargs["Body"] == TINY_PDF
    assert call_kwargs["ContentType"] == "application/pdf"


@pytest.mark.asyncio
async def test_upload_enqueues_arq_job(client, alice_chatbot, mock_s3, mock_arq):
    cid = alice_chatbot["id"]
    resp = await client.post(
        f"/api/v1/chatbots/{cid}/documents",
        files={"file": ("report.pdf", TINY_PDF, "application/pdf")},
        headers=HEADERS_ALICE,
    )
    doc_id = resp.json()["document"]["id"]
    mock_arq.enqueue_job.assert_called_once_with("ingest_document", document_id=doc_id)


@pytest.mark.asyncio
async def test_upload_unsupported_mime_returns_415(client, alice_chatbot, mock_s3, mock_arq):
    cid = alice_chatbot["id"]
    resp = await client.post(
        f"/api/v1/chatbots/{cid}/documents",
        files={"file": ("image.png", b"\x89PNG\r\n", "image/png")},
        headers=HEADERS_ALICE,
    )
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "unsupported_media_type"


@pytest.mark.asyncio
async def test_upload_too_large_returns_413(client, alice_chatbot, mock_s3, mock_arq):
    cid = alice_chatbot["id"]
    big_file = b"x" * (20 * 1024 * 1024 + 1)
    resp = await client.post(
        f"/api/v1/chatbots/{cid}/documents",
        files={"file": ("big.pdf", big_file, "application/pdf")},
        headers=HEADERS_ALICE,
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"


@pytest.mark.asyncio
async def test_upload_to_wrong_tenant_chatbot_returns_404(client, alice_chatbot, mock_s3, mock_arq):
    cid = alice_chatbot["id"]
    resp = await client.post(
        f"/api/v1/chatbots/{cid}/documents",
        files={"file": ("test.pdf", TINY_PDF, "application/pdf")},
        headers=HEADERS_BOB,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_upload_requires_auth(client, alice_chatbot):
    cid = alice_chatbot["id"]
    resp = await client.post(
        f"/api/v1/chatbots/{cid}/documents",
        files={"file": ("test.pdf", TINY_PDF, "application/pdf")},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/v1/chatbots/{chatbot_id}/documents
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_documents_returns_paginated_shape(client, alice_chatbot, mock_s3, mock_arq):
    cid = alice_chatbot["id"]
    await client.post(
        f"/api/v1/chatbots/{cid}/documents",
        files={"file": ("a.pdf", TINY_PDF, "application/pdf")},
        headers=HEADERS_ALICE,
    )
    resp = await client.get(f"/api/v1/chatbots/{cid}/documents", headers=HEADERS_ALICE)
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert "has_more" in data
    assert data["total"] >= 1


@pytest.mark.asyncio
async def test_list_documents_shows_uploaded_doc(client, alice_chatbot, mock_s3, mock_arq):
    cid = alice_chatbot["id"]
    up = await client.post(
        f"/api/v1/chatbots/{cid}/documents",
        files={"file": ("listed.txt", TINY_TXT, "text/plain")},
        headers=HEADERS_ALICE,
    )
    doc_id = up.json()["document"]["id"]
    resp = await client.get(f"/api/v1/chatbots/{cid}/documents", headers=HEADERS_ALICE)
    ids = [d["id"] for d in resp.json()["items"]]
    assert doc_id in ids


@pytest.mark.asyncio
async def test_list_documents_limit_too_large_returns_422(client, alice_chatbot):
    cid = alice_chatbot["id"]
    resp = await client.get(
        f"/api/v1/chatbots/{cid}/documents?limit=200",
        headers=HEADERS_ALICE,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_documents_wrong_chatbot_returns_404(client):
    resp = await client.get(
        f"/api/v1/chatbots/{uuid.uuid4()}/documents",
        headers=HEADERS_ALICE,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_tenant_isolation(client, alice_chatbot, mock_s3, mock_arq):
    cid = alice_chatbot["id"]
    await client.post(
        f"/api/v1/chatbots/{cid}/documents",
        files={"file": ("secret.pdf", TINY_PDF, "application/pdf")},
        headers=HEADERS_ALICE,
    )
    resp = await client.get(f"/api/v1/chatbots/{cid}/documents", headers=HEADERS_BOB)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/v1/chatbots/{chatbot_id}/documents/{document_id}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_document_returns_204(client, alice_chatbot, mock_s3, mock_arq):
    cid = alice_chatbot["id"]
    up = await client.post(
        f"/api/v1/chatbots/{cid}/documents",
        files={"file": ("del.pdf", TINY_PDF, "application/pdf")},
        headers=HEADERS_ALICE,
    )
    doc_id = up.json()["document"]["id"]
    resp = await client.delete(
        f"/api/v1/chatbots/{cid}/documents/{doc_id}",
        headers=HEADERS_ALICE,
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_removes_from_s3(client, alice_chatbot, mock_s3, mock_arq):
    cid = alice_chatbot["id"]
    up = await client.post(
        f"/api/v1/chatbots/{cid}/documents",
        files={"file": ("del.pdf", TINY_PDF, "application/pdf")},
        headers=HEADERS_ALICE,
    )
    doc_id = up.json()["document"]["id"]
    await client.delete(
        f"/api/v1/chatbots/{cid}/documents/{doc_id}",
        headers=HEADERS_ALICE,
    )
    mock_s3.delete_object.assert_called_once()


@pytest.mark.asyncio
async def test_delete_soft_deletes_document(client, alice_chatbot, mock_s3, mock_arq):
    cid = alice_chatbot["id"]
    up = await client.post(
        f"/api/v1/chatbots/{cid}/documents",
        files={"file": ("gone.txt", TINY_TXT, "text/plain")},
        headers=HEADERS_ALICE,
    )
    doc_id = up.json()["document"]["id"]
    await client.delete(
        f"/api/v1/chatbots/{cid}/documents/{doc_id}",
        headers=HEADERS_ALICE,
    )
    list_resp = await client.get(f"/api/v1/chatbots/{cid}/documents", headers=HEADERS_ALICE)
    ids = [d["id"] for d in list_resp.json()["items"]]
    assert doc_id not in ids


@pytest.mark.asyncio
async def test_delete_wrong_tenant_returns_404(client, alice_chatbot, mock_s3, mock_arq):
    cid = alice_chatbot["id"]
    up = await client.post(
        f"/api/v1/chatbots/{cid}/documents",
        files={"file": ("steal.pdf", TINY_PDF, "application/pdf")},
        headers=HEADERS_ALICE,
    )
    doc_id = up.json()["document"]["id"]
    resp = await client.delete(
        f"/api/v1/chatbots/{cid}/documents/{doc_id}",
        headers=HEADERS_BOB,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_nonexistent_returns_404(client, alice_chatbot):
    cid = alice_chatbot["id"]
    resp = await client.delete(
        f"/api/v1/chatbots/{cid}/documents/{uuid.uuid4()}",
        headers=HEADERS_ALICE,
    )
    assert resp.status_code == 404
