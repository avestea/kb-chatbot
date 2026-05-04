from src.rag.retrieve import RetrievedChunk

PROMPT_VERSION = "v1"

SYSTEM_TEMPLATE = """\
You are a helpful assistant for {chatbot_name}.
Answer questions based ONLY on the following context retrieved from the knowledge base.
If the answer is not found in the context, say "I don't have information about that in my knowledge base."
Do not make up information. Be concise and direct.

Context:
{context}
"""


def build_system_prompt(chatbot_name: str, chunks: list[RetrievedChunk]) -> str:
    if chunks:
        context = "\n".join(
            f"[{i+1}] ({c.document_name}) {c.content}"
            for i, c in enumerate(chunks)
        )
    else:
        context = "(no relevant context found)"
    return SYSTEM_TEMPLATE.format(chatbot_name=chatbot_name, context=context)
