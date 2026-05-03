"""Factories for creating test data."""
import uuid


class TenantFactory:
    @staticmethod
    def create(name: str = "Test Tenant", plan: str = "free") -> dict:
        return {
            "id": str(uuid.uuid4()),
            "name": name,
            "plan": plan,
            "clerk_user_id": f"test_{uuid.uuid4().hex[:8]}",
            "stripe_customer_id": None,
            "stripe_subscription_id": None,
        }


class ChatbotFactory:
    @staticmethod
    def create(tenant_id: str = None, name: str = "Test Chatbot") -> dict:
        return {
            "id": str(uuid.uuid4()),
            "tenant_id": tenant_id or str(uuid.uuid4()),
            "name": name,
            "system_prompt_override": None,
        }


class DocumentFactory:
    @staticmethod
    def create(chatbot_id: str = None, tenant_id: str = None, filename: str = "test.pdf", status: str = "pending") -> dict:
        return {
            "id": str(uuid.uuid4()),
            "chatbot_id": chatbot_id or str(uuid.uuid4()),
            "tenant_id": tenant_id or str(uuid.uuid4()),
            "filename": filename,
            "mime_type": "application/pdf",
            "s3_key": f"test-{uuid.uuid4().hex[:8]}.pdf",
            "status": status,
            "page_count": None,
            "error_reason": None,
        }


class MessageFactory:
    @staticmethod
    def create(conversation_id: str = None, role: str = "assistant", content: str = "") -> dict:
        return {
            "id": str(uuid.uuid4()),
            "conversation_id": conversation_id or str(uuid.uuid4()),
            "role": role,
            "content": content,
            "source_chunk_ids": None,
            "tokens_used": None,
            "no_answer": False,
            "prompt_version": None,
        }
