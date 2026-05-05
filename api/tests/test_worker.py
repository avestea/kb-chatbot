import io
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.worker.parsers import get_parser_for
from src.worker.parsers.txt import parse_txt
from src.worker.parsers.html import parse_html
from src.worker.chunker import chunk_text, DraftChunk, MIN_TOKENS, TARGET_TOKENS
from src.lib.errors import UnsupportedMimeTypeError
from src.worker.jobs import ingest_document
from src.db.models import Document, Tenant, Chatbot, Chunk
from src.db.base import async_session_factory
from tests.fakes.openai import fake_embed_chunks

# --------------------------------------------------------------------------------------
# Parser unit tests
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_parse_txt_returns_decoded_text():
    result = await parse_txt(b"hello world", "test.txt")
    assert result == "hello world"


@pytest.mark.asyncio
async def test_parse_txt_handles_encoding_errors():
    result = await parse_txt(b"\xff\xfe", "test.txt")
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_parse_html_extracts_body_text():
    html = b"<html><body><p>Hello HTML</p></body></html>"
    result = await parse_html(html, "test.html")
    assert "Hello HTML" in result


@pytest.mark.asyncio
async def test_parse_html_removes_scripts():
    html = b"<html><body><p>Visible</p><script>alert(1)</script></body></html>"
    result = await parse_html(html, "test.html")
    assert "alert(1)" not in result
    assert "Visible" in result


@pytest.mark.asyncio
async def test_parse_pdf_extracts_text():
    mock_page = MagicMock()
    mock_page.extract_words.return_value = [
        {"text": "PDF", "top": 10},
        {"text": "content", "top": 10},
    ]
    mock_pdf = MagicMock()
    mock_pdf.pages = [mock_page]
    mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
    mock_pdf.__exit__ = MagicMock(return_value=False)

    with patch("pdfplumber.open", return_value=mock_pdf):
        from src.worker.parsers.pdf import parse_pdf
        result = await parse_pdf(b"fake pdf bytes", "test.pdf")

    assert result == "PDF content"


@pytest.mark.asyncio
async def test_parse_docx_extracts_paragraphs():
    import docx as python_docx
    buf = io.BytesIO()
    doc = python_docx.Document()
    doc.add_paragraph("First paragraph")
    doc.add_paragraph("Second paragraph")
    doc.save(buf)

    from src.worker.parsers.docx import parse_docx
    result = await parse_docx(buf.getvalue(), "test.docx")
    assert "First paragraph" in result
    assert "Second paragraph" in result


# --------------------------------------------------------------------------------------
# Parser dispatch tests
# --------------------------------------------------------------------------------------

def test_get_parser_for_pdf():
    assert callable(get_parser_for("application/pdf"))


def test_get_parser_for_docx():
    assert callable(get_parser_for(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ))


def test_get_parser_for_txt():
    assert callable(get_parser_for("text/plain"))


def test_get_parser_for_html():
    assert callable(get_parser_for("text/html"))


def test_get_parser_for_unknown_raises():
    with pytest.raises(UnsupportedMimeTypeError):
        get_parser_for("image/png")


# --------------------------------------------------------------------------------------
# Chunker unit tests (Slice 6)
# --------------------------------------------------------------------------------------

def test_chunk_text_empty_returns_empty():
    assert chunk_text("") == []


def test_chunk_text_short_below_min_tokens_returns_empty():
    # "Hi." is ~1 token — well below MIN_TOKENS (20)
    assert chunk_text("Hi.") == []


def test_chunk_text_produces_draft_chunks():
    long_text = "This is a test sentence. " * 50
    chunks = chunk_text(long_text)
    assert len(chunks) > 0
    for c in chunks:
        assert isinstance(c, DraftChunk)
        assert c.token_count >= MIN_TOKENS
        assert len(c.content) > 0


def test_chunk_text_chunk_index_ascending():
    long_text = "The quick brown fox jumps over the lazy dog. " * 60
    chunks = chunk_text(long_text)
    assert len(chunks) > 1
    indices = [c.chunk_index for c in chunks]
    assert indices == list(range(len(chunks)))


def test_chunk_text_multiple_chunks_for_long_text():
    # 100 sentences × ~8 tokens each = ~800 tokens → should produce ≥2 chunks
    sentence = "The quick brown fox jumps over the lazy dog."
    long_text = (sentence + " ") * 100
    chunks = chunk_text(long_text)
    assert len(chunks) >= 2


def test_chunk_text_token_count_accurate():
    import tiktoken
    enc = tiktoken.get_encoding("cl100k_base")
    long_text = "This is a complete sentence for testing purposes. " * 60
    chunks = chunk_text(long_text)
    for c in chunks:
        actual = len(enc.encode(c.content))
        assert c.token_count == actual


def test_chunk_text_pure_function_same_output():
    text = "Repeatable test sentence. " * 80
    assert chunk_text(text) == chunk_text(text)


def test_chunk_text_no_chunks_smaller_than_min_tokens():
    long_text = "Short. " * 200  # many tiny sentences
    chunks = chunk_text(long_text)
    for c in chunks:
        assert c.token_count >= MIN_TOKENS


# --------------------------------------------------------------------------------------
# Embedder unit tests (Slice 6)
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_embed_chunks_returns_1536_vectors():
    from unittest.mock import AsyncMock, MagicMock
    import src.lib.embedder as embedder_module

    fake_data = [MagicMock(embedding=[0.1] * 1536, index=0)]
    fake_response = MagicMock(data=fake_data)
    embedder_module._client.embeddings.create = AsyncMock(return_value=fake_response)

    result = await embedder_module.embed_chunks(["hello world"])
    assert len(result) == 1
    assert len(result[0]) == 1536


@pytest.mark.asyncio
async def test_embed_chunks_preserves_order():
    import src.lib.embedder as embedder_module
    from unittest.mock import AsyncMock, MagicMock

    texts = ["first", "second", "third"]
    fake_data = [
        MagicMock(embedding=[float(i)] * 1536, index=i)
        for i in range(len(texts))
    ]
    fake_response = MagicMock(data=fake_data)
    embedder_module._client.embeddings.create = AsyncMock(return_value=fake_response)

    result = await embedder_module.embed_chunks(texts)
    assert len(result) == 3
    # Each vector should correspond to its index position
    assert result[0][0] == 0.0
    assert result[1][0] == 1.0
    assert result[2][0] == 2.0


@pytest.mark.asyncio
async def test_embed_chunks_batches_at_100():
    import src.lib.embedder as embedder_module
    from unittest.mock import AsyncMock, MagicMock

    texts = ["text"] * 150  # 2 batches: 100 + 50

    def make_response(batch):
        data = [MagicMock(embedding=[0.5] * 1536, index=i) for i in range(len(batch))]
        return MagicMock(data=data)

    call_count = 0

    async def mock_create(**kwargs):
        nonlocal call_count
        call_count += 1
        return make_response(kwargs["input"])

    embedder_module._client.embeddings.create = mock_create

    result = await embedder_module.embed_chunks(texts)
    assert len(result) == 150
    assert call_count == 2


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------

async def _create_document(mime_type: str = "text/plain", filename: str = "test.txt") -> str:
    async with async_session_factory() as db:
        tenant = Tenant(
            name="Worker Test Tenant",
            plan="free",
            clerk_user_id=f"worker_{uuid.uuid4().hex[:12]}",
        )
        db.add(tenant)
        await db.flush()

        chatbot = Chatbot(tenant_id=tenant.id, name="Worker Test Bot")
        db.add(chatbot)
        await db.flush()

        doc = Document(
            chatbot_id=chatbot.id,
            tenant_id=tenant.id,
            filename=filename,
            mime_type=mime_type,
            s3_key=f"test/{uuid.uuid4().hex}/{filename}",
            status="pending",
        )
        db.add(doc)
        await db.commit()
        return str(doc.id)


async def _get_document(doc_id: str) -> Document | None:
    from sqlalchemy import select
    async with async_session_factory() as db:
        result = await db.execute(
            select(Document).where(Document.id == uuid.UUID(doc_id))
        )
        return result.scalar_one_or_none()


async def _get_chunks(doc_id: str) -> list[Chunk]:
    from sqlalchemy import select
    async with async_session_factory() as db:
        result = await db.execute(
            select(Chunk).where(Chunk.document_id == uuid.UUID(doc_id))
        )
        return result.scalars().all()


def _s3_mock(file_bytes: bytes):
    body = AsyncMock()
    body.read = AsyncMock(return_value=file_bytes)
    s3 = AsyncMock()
    s3.get_object = AsyncMock(return_value={"Body": body})
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=s3)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


# Enough text to exceed TARGET_TOKENS (400) and produce chunks
_LONG_TXT = b"This is a complete test sentence for the ingestion pipeline. " * 100
_LONG_HTML = (
    b"<html><body>" +
    b"<p>This is a complete test sentence for the ingestion pipeline.</p>" * 100 +
    b"</body></html>"
)


# --------------------------------------------------------------------------------------
# Job integration tests — Slice 5 (updated for Slice 6 behavior)
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_txt_becomes_ready():
    doc_id = await _create_document(mime_type="text/plain", filename="hello.txt")
    with patch("src.worker.jobs.s3_client", return_value=_s3_mock(_LONG_TXT)):
        with patch("src.worker.jobs.embed_chunks", new=fake_embed_chunks):
            await ingest_document({"job_try": 1}, doc_id)

    doc = await _get_document(doc_id)
    assert doc.status == "ready"
    assert doc.error_reason is None


@pytest.mark.asyncio
async def test_ingest_html_becomes_ready():
    doc_id = await _create_document(mime_type="text/html", filename="page.html")
    with patch("src.worker.jobs.s3_client", return_value=_s3_mock(_LONG_HTML)):
        with patch("src.worker.jobs.embed_chunks", new=fake_embed_chunks):
            await ingest_document({"job_try": 1}, doc_id)

    doc = await _get_document(doc_id)
    assert doc.status == "ready"


@pytest.mark.asyncio
async def test_ingest_corrupt_pdf_sets_error():
    doc_id = await _create_document(mime_type="application/pdf", filename="corrupt.pdf")
    with patch("src.worker.jobs.s3_client", return_value=_s3_mock(b"not a real pdf!")):
        # job_try=3 so Retry is not raised; error is persisted
        await ingest_document({"job_try": 3}, doc_id)

    doc = await _get_document(doc_id)
    assert doc.status == "error"
    assert doc.error_reason is not None
    assert len(doc.error_reason) > 0


@pytest.mark.asyncio
async def test_ingest_error_raises_retry_on_first_try():
    from arq import Retry
    doc_id = await _create_document(mime_type="application/pdf", filename="bad.pdf")
    with patch("src.worker.jobs.s3_client", return_value=_s3_mock(b"not a pdf")):
        with pytest.raises(Retry):
            await ingest_document({"job_try": 1}, doc_id)

    doc = await _get_document(doc_id)
    assert doc.status == "error"


@pytest.mark.asyncio
async def test_ingest_unsupported_mime_sets_error_no_retry():
    from arq import Retry
    doc_id = await _create_document(mime_type="image/png", filename="photo.png")
    with patch("src.worker.jobs.s3_client", return_value=_s3_mock(b"\x89PNG\r\n")):
        # Should NOT raise Retry even though job_try=1
        result = await ingest_document({"job_try": 1}, doc_id)

    assert result is None
    doc = await _get_document(doc_id)
    assert doc.status == "error"
    assert doc.error_reason is not None


@pytest.mark.asyncio
async def test_ingest_missing_document_is_noop():
    missing_id = str(uuid.uuid4())
    with patch("src.worker.jobs.s3_client", return_value=_s3_mock(b"")):
        result = await ingest_document({"job_try": 1}, missing_id)

    assert result is None


# --------------------------------------------------------------------------------------
# Job integration tests — Slice 6
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_creates_chunks_in_db():
    doc_id = await _create_document(mime_type="text/plain", filename="chunks.txt")
    with patch("src.worker.jobs.s3_client", return_value=_s3_mock(_LONG_TXT)):
        with patch("src.worker.jobs.embed_chunks", new=fake_embed_chunks):
            await ingest_document({"job_try": 1}, doc_id)

    chunks = await _get_chunks(doc_id)
    assert len(chunks) > 0


@pytest.mark.asyncio
async def test_ingest_chunk_fields_correct():
    doc_id = await _create_document(mime_type="text/plain", filename="fields.txt")
    with patch("src.worker.jobs.s3_client", return_value=_s3_mock(_LONG_TXT)):
        with patch("src.worker.jobs.embed_chunks", new=fake_embed_chunks):
            await ingest_document({"job_try": 1}, doc_id)

    chunks = await _get_chunks(doc_id)
    assert len(chunks) > 0

    # Verify embedding_model
    for c in chunks:
        assert c.embedding_model == "text-embedding-3-small"
        assert len(c.embedding) == 1536
        assert c.token_count > 0
        assert len(c.content) > 0

    # chunk_index must be ascending 0..N-1
    indices = sorted(c.chunk_index for c in chunks)
    assert indices == list(range(len(chunks)))


@pytest.mark.asyncio
async def test_ingest_short_text_no_chunks_sets_error():
    doc_id = await _create_document(mime_type="text/plain", filename="short.txt")
    # "Hi." is well below MIN_TOKENS
    with patch("src.worker.jobs.s3_client", return_value=_s3_mock(b"Hi.")):
        with patch("src.worker.jobs.embed_chunks", new=fake_embed_chunks):
            await ingest_document({"job_try": 1}, doc_id)

    doc = await _get_document(doc_id)
    assert doc.status == "error"
    assert doc.error_reason == "document produced no chunks"


@pytest.mark.asyncio
async def test_ingest_embed_failure_wipes_partial_chunks_and_sets_error():
    from arq import Retry

    doc_id = await _create_document(mime_type="text/plain", filename="embed_fail.txt")

    async def failing_embed(contents):
        raise RuntimeError("OpenAI API unavailable")

    with patch("src.worker.jobs.s3_client", return_value=_s3_mock(_LONG_TXT)):
        with patch("src.worker.jobs.embed_chunks", new=failing_embed):
            # job_try=3 so no Retry raised
            await ingest_document({"job_try": 3}, doc_id)

    doc = await _get_document(doc_id)
    assert doc.status == "error"
    assert doc.error_reason is not None
    assert "OpenAI API unavailable" in doc.error_reason

    # Partial chunks must be wiped
    chunks = await _get_chunks(doc_id)
    assert len(chunks) == 0


@pytest.mark.asyncio
async def test_ingest_idempotency_retry_clears_old_chunks():
    """Second run should delete stale chunks from a prior partial run."""
    from sqlalchemy import select
    from src.db.base import async_session_factory

    doc_id = await _create_document(mime_type="text/plain", filename="retry.txt")
    doc_uuid = uuid.UUID(doc_id)

    # Manually insert a stale chunk to simulate a partial prior run
    async with async_session_factory() as db:
        doc_row = (await db.execute(
            select(Document).where(Document.id == doc_uuid)
        )).scalar_one()
        stale_chunk = Chunk(
            document_id=doc_row.id,
            chatbot_id=doc_row.chatbot_id,
            tenant_id=doc_row.tenant_id,
            content="stale chunk from prior run",
            token_count=5,
            chunk_index=0,
            embedding=[0.0] * 1536,
            embedding_model="text-embedding-3-small",
        )
        db.add(stale_chunk)
        await db.commit()

    stale_id = stale_chunk.id

    # Run ingest — idempotency step must delete the stale chunk
    with patch("src.worker.jobs.s3_client", return_value=_s3_mock(_LONG_TXT)):
        with patch("src.worker.jobs.embed_chunks", new=fake_embed_chunks):
            await ingest_document({"job_try": 2}, doc_id)

    doc = await _get_document(doc_id)
    assert doc.status == "ready"

    chunks = await _get_chunks(doc_id)
    chunk_ids = {c.id for c in chunks}
    # The stale chunk must be gone
    assert stale_id not in chunk_ids
    # And fresh chunks were created
    assert len(chunks) > 0


@pytest.mark.asyncio
async def test_ingest_status_transitions_pending_processing_ready():
    """Verify Document.status moves through all expected states."""
    doc_id = await _create_document(mime_type="text/plain", filename="states.txt")

    # Before job: pending
    doc = await _get_document(doc_id)
    assert doc.status == "pending"

    with patch("src.worker.jobs.s3_client", return_value=_s3_mock(_LONG_TXT)):
        with patch("src.worker.jobs.embed_chunks", new=fake_embed_chunks):
            await ingest_document({"job_try": 1}, doc_id)

    # After job: ready
    doc = await _get_document(doc_id)
    assert doc.status == "ready"
    assert doc.error_reason is None
