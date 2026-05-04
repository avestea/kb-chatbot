"""Fake OpenAI embedder for testing — returns deterministic 1536-dim vectors."""

FAKE_VECTOR: list[float] = [0.1] * 1536


async def fake_embed_chunks(contents: list[str]) -> list[list[float]]:
    """Drop-in replacement for embed_chunks that never calls the OpenAI API."""
    return [FAKE_VECTOR[:] for _ in contents]
