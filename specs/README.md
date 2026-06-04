# specs/

This directory contains the architecture document and the LLM-driven build prompts used to construct this project slice by slice.

## Files

| File | Purpose |
|---|---|
| `kb-chatbot-architecture.md` | Full architecture specification written before implementation began. Covers tech stack decisions, data model, API contracts, and the retrieval algorithm. |
| `progress.md` | Build log tracking the status of each slice, deviations from the original spec, and what exists at the current state of the codebase. |
| `slices/00-prompt-prefix.md` | Shared context prepended to every slice prompt. Defines the tech stack, data model, cross-slice function signatures, and conventions all implementer agents must follow. |
| `slices/slice-NN-*.md` | One file per feature slice. Each file is a self-contained prompt that, combined with the prefix, tells the implementer exactly what to build, what tests to write, and what the acceptance criteria are. |

## How this project was built

The codebase was implemented incrementally using 16 vertical slices (0–15). Each slice is a focused unit of work — a set of deliverables, acceptance criteria, and tests — small enough to be implemented in a single LLM session.

The build sequence:

```
Slice 0   Docker / dev environment
Slice 1   FastAPI scaffold + DB schema (6 tables, pgvector, Alembic)
Slice 2   Auth & multi-tenancy (demo mode + Clerk stub)
Slice 3   Chatbot CRUD
Slice 4   Document upload (S3/MinIO + ARQ job enqueue)
Slice 5   Background parsing worker (PDF, DOCX, HTML, TXT)
Slice 6   Chunk + embed + persist (tiktoken + OpenAI embeddings)
Slice 7   Retrieval function (pgvector cosine search)
Slice 8   Chat endpoint (SSE streaming via Claude Sonnet)
Slice 9   Gradio UI (Chatbots / Documents / Chat tabs)
Slice 10  Explainability (sources SSE event + source_chunks persistence)
Slice 11  Evaluation dashboard (analytics routes + Gradio Evaluation tab)
Slice 12  Hybrid search (BM25 + vector + Reciprocal Rank Fusion)
Slice 13  Query rewriting (Claude Haiku rewrites follow-up questions)
Slice 14  Feedback (thumbs up/down per message, satisfaction_rate metric)
Slice 15  Observability & cost tracking (per-call token + USD logging)
```

Slices 0–9 are the MVP. Slices 10–15 are post-MVP feature extensions.

## Using the slice prompts

Each slice file is a standalone LLM prompt. To reproduce or extend the build:

1. Paste `slices/00-prompt-prefix.md` first (shared context).
2. Then paste the slice file you want to implement.
3. The implementer should only build what is listed in that slice's deliverables.

The `progress.md` file documents any deviations made during implementation — places where the spec was updated, simplified, or corrected based on what was actually built.
