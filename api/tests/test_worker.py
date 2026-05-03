import io
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.worker.parsers import get_parser_for
from src.worker.parsers.txt import parse_txt
from src.worker.parsers.html import parse_html
from src.lib.errors import UnsupportedMimeTypeError
from src.worker.jobs import ingest_document
from src.db.models import Document, Tenant, Chatbot
from src.db.base import async_session_factory


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
    mock_page.extract_text.return_value = "PDF content"
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


def _s3_mock(file_bytes: bytes):
    body = AsyncMock()
    body.read = AsyncMock(return_value=file_bytes)
    s3 = AsyncMock()
    s3.get_object = AsyncMock(return_value={"Body": body})
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=s3)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


# --------------------------------------------------------------------------------------
# Job integration tests
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_txt_becomes_processing():
    doc_id = await _create_document(mime_type="text/plain", filename="hello.txt")
    with patch("src.worker.jobs.s3_client", return_value=_s3_mock(b"Hello text content")):
        await ingest_document({"job_try": 1}, doc_id)

    doc = await _get_document(doc_id)
    assert doc.status == "processing"
    assert doc.error_reason is None


@pytest.mark.asyncio
async def test_ingest_html_becomes_processing():
    doc_id = await _create_document(mime_type="text/html", filename="page.html")
    with patch("src.worker.jobs.s3_client", return_value=_s3_mock(
        b"<html><body><p>Test page</p></body></html>"
    )):
        await ingest_document({"job_try": 1}, doc_id)

    doc = await _get_document(doc_id)
    assert doc.status == "processing"


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
