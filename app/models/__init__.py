# Import every model here so `Base.metadata` sees it (needed by Alembic autogenerate).
from app.models.chat import Conversation, Message
from app.models.user import User

__all__ = ["Conversation", "Message", "User"]
