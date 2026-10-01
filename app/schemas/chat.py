from datetime import datetime

from pydantic import BaseModel, Field


class ChatSend(BaseModel):
    """One user turn.

    Leave `thread_id` out to start a new conversation; the reply carries the id to reuse
    on the next turn. The conversation history lives in the LLM service, so the client
    never has to resend previous messages.
    """

    message: str = Field(min_length=1, max_length=4000)
    thread_id: str | None = None

    model_config = {
        "json_schema_extra": {
            "examples": [{"message": "Đi Đà Lạt 3 ngày, 2 người lớn 1 bé, ngân sách 15tr?"}]
        }
    }


class ChatReply(BaseModel):
    thread_id: str
    reply: str


class ChatMessage(BaseModel):
    role: str = Field(description="human | ai | system")
    content: str


class ChatHistory(BaseModel):
    thread_id: str
    messages: list[ChatMessage]


class ChatThread(BaseModel):
    thread_id: str
    last_active_at: datetime
