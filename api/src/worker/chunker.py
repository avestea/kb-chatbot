import re
from dataclasses import dataclass
import tiktoken

@dataclass
class DraftChunk:
    content: str
    token_count: int
    chunk_index: int

_enc = tiktoken.get_encoding("cl100k_base")

TARGET_TOKENS = 400
OVERLAP_TOKENS = 50
MIN_TOKENS = 20


def chunk_text(text: str) -> list[DraftChunk]:
    """Pure function. No I/O. Same input → same output."""
    sentences = re.split(r'(?<=[.!?])\s+', text)
    sentences = [s.strip() for s in sentences if s.strip()]

    chunks: list[DraftChunk] = []
    current: list[str] = []
    current_tokens = 0
    chunk_index = 0

    i = 0
    while i < len(sentences):
        sentence = sentences[i]
        tokens = len(_enc.encode(sentence))

        if current_tokens + tokens >= TARGET_TOKENS and current:
            content = " ".join(current)
            token_count = len(_enc.encode(content))
            if token_count >= MIN_TOKENS:
                chunks.append(DraftChunk(
                    content=content,
                    token_count=token_count,
                    chunk_index=chunk_index,
                ))
                chunk_index += 1

            # Overlap: walk back until we have ~50 overlap tokens
            overlap: list[str] = []
            overlap_tokens = 0
            for sent in reversed(current):
                t = len(_enc.encode(sent))
                if overlap_tokens + t > OVERLAP_TOKENS:
                    break
                overlap.insert(0, sent)
                overlap_tokens += t

            current = overlap
            current_tokens = overlap_tokens
        else:
            current.append(sentence)
            current_tokens += tokens
            i += 1

    # Final chunk
    if current:
        content = " ".join(current)
        token_count = len(_enc.encode(content))
        if token_count >= MIN_TOKENS:
            chunks.append(DraftChunk(
                content=content,
                token_count=token_count,
                chunk_index=chunk_index,
            ))

    return chunks
