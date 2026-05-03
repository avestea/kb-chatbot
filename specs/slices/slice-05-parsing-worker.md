# Slice 5 — Ingestion Worker Shell & Parsing

**Depends on:** Slice 1, Slice 4
**Prereq:** Read `00-prompt-prefix.md` first.

> Slice 5 sets up the ARQ worker and turns uploaded files into plain text. Slice 6 chunks, embeds, and persists. They extend the same worker entrypoint.

---

## Goal

An ARQ worker process picks up `ingest_document` jobs, downloads the file from S3, and converts it to plain text. Status transitions are wired. No chunking or embedding yet.

## Deliverables

- `api/src/worker.py` — ARQ `WorkerSettings` entrypoint.
- `api/src/worker/jobs.py` — `ingest_document` async job function.
- `api/src/worker/parsers/__init__.py` — MIME → parser dispatch.
- `api/src/worker/parsers/pdf.py` — pdfplumber.
- `api/src/worker/parsers/docx.py` — python-docx.
- `api/src/worker/parsers/html.py` — BeautifulSoup4.
- `api/src/worker/parsers/txt.py` — UTF-8 decode.

## Worker entrypoint (`api/src/worker.py`)

```python
from arq.connections import RedisSettings
from src.config.env import settings
from src.worker.jobs import ingest_document

class WorkerSettings:
    functions = [ingest_document]
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    max_jobs = 1           # process one document at a time
    job_timeout = 600      # 10 minutes max per job
    keep_result = 3600
    allow_abort_jobs = True
```

Run with: `arq src.worker.WorkerSettings`

## Job handler (`api/src/worker/jobs.py`)

```python
import re
from arq import Retry
from sqlalchemy import select, update
from src.db.base import async_session
from src.db.models import Document
from src.lib.s3 import s3_client
from src.config.env import settings
from src.worker.parsers import get_parser_for
from src.lib.log import log

async def ingest_document(ctx: dict, document_id: str):
    """
    ARQ job: download from S3, parse to text, update status.
    Slice 6 extends this function to add chunking + embedding.
    """
    job_try = ctx.get("job_try", 1)

    async with async_session() as db:
        doc = (await db.execute(
            select(Document).where(Document.id == document_id)
        )).scalar_one_or_none()

        if doc is None:
            log.warning("document not found, acking", document_id=document_id)
            return  # don't retry — document was deleted

        # Mark processing
        doc.status = "processing"
        doc.error_reason = None
        await db.commit()

        try:
            # Download from S3
            async with s3_client() as s3:
                response = await s3.get_object(Bucket=settings.S3_BUCKET, Key=doc.s3_key)
                file_bytes = await response["Body"].read()

            # Parse to plain text
            parser = get_parser_for(doc.mime_type)
            text = await parser(file_bytes, doc.filename)
            text = _normalize_text(text)

            log.info("document parsed", document_id=document_id, text_length=len(text))

            # Slice 6 will continue from here with chunk + embed + persist
            # For now: leave status as 'processing' — Slice 6 sets it to 'ready'

        except Exception as exc:
            err_msg = str(exc)[:500]
            log.error("ingest failed", document_id=document_id, error=err_msg, exc_info=exc)

            async with async_session() as db2:
                await db2.execute(
                    update(Document)
                    .where(Document.id == document_id)
                    .values(status="error", error_reason=err_msg)
                )
                await db2.commit()

            if job_try < 3:
                raise Retry(defer=5 ** job_try)  # 5s, 25s
            # After 3 tries: job fails permanently (already set status=error)

def _normalize_text(text: str) -> str:
    text = text.strip()
    text = re.sub(r'\n{3,}', '\n\n', text)  # collapse excess blank lines
    return text
```

## Parser dispatch (`api/src/worker/parsers/__init__.py`)

```python
from typing import Callable, Awaitable
from src.lib.errors import UnsupportedMimeTypeError

Parser = Callable[[bytes, str], Awaitable[str]]

_PARSERS: dict[str, Parser] = {}

def get_parser_for(mime_type: str) -> Parser:
    parser = _PARSERS.get(mime_type)
    if parser is None:
        raise UnsupportedMimeTypeError(mime_type)
    return parser

# Register parsers (imported at module bottom to avoid circular imports)
def _register():
    from src.worker.parsers.pdf import parse_pdf
    from src.worker.parsers.docx import parse_docx
    from src.worker.parsers.html import parse_html
    from src.worker.parsers.txt import parse_txt

    _PARSERS["application/pdf"] = parse_pdf
    _PARSERS["application/vnd.openxmlformats-officedocument.wordprocessingml.document"] = parse_docx
    _PARSERS["text/html"] = parse_html
    _PARSERS["text/plain"] = parse_txt

_register()
```

## PDF parser (`api/src/worker/parsers/pdf.py`)

```python
import pdfplumber
import io

async def parse_pdf(data: bytes, filename: str) -> str:
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = []
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
    return "\n\n".join(pages)
```

## DOCX parser (`api/src/worker/parsers/docx.py`)

```python
import docx
import io

async def parse_docx(data: bytes, filename: str) -> str:
    document = docx.Document(io.BytesIO(data))
    paragraphs = [p.text for p in document.paragraphs if p.text.strip()]
    return "\n\n".join(paragraphs)
```

## HTML parser (`api/src/worker/parsers/html.py`)

```python
from bs4 import BeautifulSoup

async def parse_html(data: bytes, filename: str) -> str:
    soup = BeautifulSoup(data, "lxml")
    # Remove non-content tags
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)
```

## TXT parser (`api/src/worker/parsers/txt.py`)

```python
async def parse_txt(data: bytes, filename: str) -> str:
    return data.decode("utf-8", errors="replace")
```

## Implementation notes

- ARQ's `ctx["job_try"]` starts at 1 on first attempt; increment by 1 per retry.
- `Retry(defer=seconds)` tells ARQ to re-queue the job after a delay. After `max_tries` (set in WorkerSettings or per-function), ARQ marks the job as failed without retrying.
- Unsupported MIME type should not retry — if you catch `UnsupportedMimeTypeError` in the handler, set `status: error` and return without raising `Retry`.
- pdfplumber is more reliable than pypdf/PyPDF2 for text extraction from complex PDFs.

## Acceptance criteria

- Worker boots via `docker compose up worker` and idles waiting for jobs.
- Upload a PDF → after a few seconds, worker logs show extracted text length and document `status` becomes `processing` (Slice 6 moves it to `ready`).
- Upload a corrupt PDF → `Document.status` becomes `error` with `error_reason` populated; worker keeps running.
- Unsupported MIME type job fails permanently (no retries), sets `status: error`.
