"""Chat history kept for good in PostgreSQL.

The LLM service still holds the agent's short-term memory (checkpoints in Redis, which
expire). These tables are the durable record: the conversation list, titles and every
message, so a conversation stays readable — and resumable — long after the LLM side
has forgotten it.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, _utcnow

# SQLite (tests) only autoincrements an INTEGER PRIMARY KEY, not BIGINT.
_BigIntId = BigInteger().with_variant(Integer(), "sqlite")


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"
    # The thread list: one user's conversations, most recently used first.
    __table_args__ = (
        Index("ix_conversations_user_id_last_active_at", "user_id", "last_active_at"),
    )

    # Same id the LLM service uses as its thread_id ("chat-<hex12>").
    id: Mapped[str] = mapped_column(String(64), primary_key=True, sort_order=-10)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<Conversation id={self.id!r} user_id={self.user_id}>"


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conversation_id_id", "conversation_id", "id"),)

    # Insertion order is conversation order, so the id doubles as the sort key.
    id: Mapped[int] = mapped_column(_BigIntId, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16))  # "human" | "ai"
    content: Mapped[str] = mapped_column(Text)
    # A trip plan built in the chat: {"form": PlanForm, "result": PlanResponse}. Lets the
    # client show the full plan (options, schedule, map) again when the conversation is
    # reopened; `content` holds the same plan as text for the assistant to read.
    plan: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<Message id={self.id} conversation_id={self.conversation_id!r} role={self.role}>"
