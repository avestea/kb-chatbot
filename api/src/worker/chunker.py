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

        # A single "sentence" can exceed TARGET_TOKENS when the source has no
        # terminal punctuation to split on -- Markdown code fences and tables
        # are the common case. Such a sentence can never be accumulated, so the
        # flush branch below would rebuild the same overlap forever without
        # advancing i. Consume it here instead, hard-split into token windows.
        if tokens >= TARGET_TOKENS:
            if current:
                content = " ".join(current)
                token_count = len(_enc.encode(content))
                if token_count >= MIN_TOKENS:
                    chunks.append(DraftChunk(
                        content=content,
                        token_count=token_count,
                        chunk_index=chunk_index,
                    ))
                    chunk_index += 1
                current = []
                current_tokens = 0

            ids = _enc.encode(sentence)
            step = TARGET_TOKENS - OVERLAP_TOKENS
            for start in range(0, len(ids), step):
                window = ids[start:start + TARGET_TOKENS]
                if len(window) < MIN_TOKENS and chunks:
                    break
                chunks.append(DraftChunk(
                    content=_enc.decode(window),
                    token_count=len(window),
                    chunk_index=chunk_index,
                ))
                chunk_index += 1
                if start + TARGET_TOKENS >= len(ids):
                    break
            i += 1
            continue

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

            # Termination guard: if the rebuilt overlap still cannot admit the
            # incoming sentence, keeping it would reproduce this exact state on
            # the next pass without advancing i. Drop it so the sentence is
            # appended instead. Triggers for sentences in
            # [TARGET_TOKENS - OVERLAP_TOKENS, TARGET_TOKENS).
            if overlap_tokens + tokens >= TARGET_TOKENS:
                overlap = []
                overlap_tokens = 0

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
