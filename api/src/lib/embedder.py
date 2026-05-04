from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from src.config.env import settings

EMBEDDING_MODEL = "text-embedding-3-small"
EmbeddingVector = list[float]

_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def _embed_batch(batch: list[str]) -> list[EmbeddingVector]:
    response = await _client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=batch,
    )
    return [item.embedding for item in sorted(response.data, key=lambda x: x.index)]


async def embed_chunks(contents: list[str]) -> list[EmbeddingVector]:
    """Batch 100 per API call. Output order matches input order."""
    BATCH = 100
    results: list[EmbeddingVector] = []
    for i in range(0, len(contents), BATCH):
        batch = contents[i:i + BATCH]
        results.extend(await _embed_batch(batch))
    return results
