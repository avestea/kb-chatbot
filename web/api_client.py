import httpx
import os
from typing import Iterator

API_BASE = os.getenv("API_BASE_URL", "http://api:8000")


class APIClient:
    def __init__(self, token: str):
        self.token = token
        self._http = httpx.AsyncClient(
            base_url=API_BASE,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )

    async def list_chatbots(self) -> list[dict]:
        r = await self._http.get("/api/v1/chatbots")
        r.raise_for_status()
        return r.json()["items"]

    async def create_chatbot(self, name: str, system_prompt: str | None = None) -> dict:
        r = await self._http.post("/api/v1/chatbots", json={
            "name": name,
            "system_prompt_override": system_prompt or None,
        })
        r.raise_for_status()
        return r.json()["chatbot"]

    async def list_documents(self, chatbot_id: str) -> list[dict]:
        r = await self._http.get(f"/api/v1/chatbots/{chatbot_id}/documents")
        r.raise_for_status()
        return r.json()["items"]

    async def upload_document(self, chatbot_id: str, file_path: str, filename: str, mime: str) -> dict:
        with open(file_path, "rb") as f:
            r = await self._http.post(
                f"/api/v1/chatbots/{chatbot_id}/documents",
                files={"file": (filename, f, mime)},
            )
        r.raise_for_status()
        return r.json()["document"]

    async def delete_document(self, chatbot_id: str, document_id: str) -> None:
        r = await self._http.delete(f"/api/v1/chatbots/{chatbot_id}/documents/{document_id}")
        r.raise_for_status()

    async def get_analytics_summary(self, chatbot_id: str | None = None) -> dict:
        params = {}
        if chatbot_id:
            params["chatbot_id"] = chatbot_id
        r = await self._http.get("/api/v1/analytics/summary", params=params)
        r.raise_for_status()
        return r.json()

    async def list_conversations(
        self,
        chatbot_id: str | None = None,
        no_answer_only: bool = False,
        limit: int = 25,
    ) -> list[dict]:
        params = {"limit": limit, "no_answer_only": str(no_answer_only).lower()}
        if chatbot_id:
            params["chatbot_id"] = chatbot_id
        r = await self._http.get("/api/v1/analytics/conversations", params=params)
        r.raise_for_status()
        return r.json()["items"]

    async def get_conversation_messages(self, conversation_id: str) -> list[dict]:
        r = await self._http.get(f"/api/v1/analytics/conversations/{conversation_id}/messages")
        r.raise_for_status()
        return r.json()["messages"]

    def chat_stream(self, chatbot_id: str, message: str, session_id: str, on_sources=None) -> Iterator[str]:
        """Synchronous generator — Gradio streaming requires sync generators."""
        import json as _json
        with httpx.Client(base_url=API_BASE, timeout=120) as c:
            with c.stream(
                "POST",
                f"/api/v1/chat/{chatbot_id}/message",
                json={"message": message, "session_id": session_id},
            ) as response:
                response.raise_for_status()
                current_event = ""
                buffer = ""
                for line in response.iter_lines():
                    if line.startswith("event: "):
                        current_event = line[7:].strip()
                    elif line.startswith("data: "):
                        try:
                            data = _json.loads(line[6:])
                        except _json.JSONDecodeError:
                            continue
                        if current_event == "sources" and on_sources:
                            on_sources(data.get("sources", []))
                        elif current_event == "token" and "text" in data:
                            buffer += data["text"]
                            yield buffer
