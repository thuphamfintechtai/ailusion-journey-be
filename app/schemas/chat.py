from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.plan import PLAN_THREAD_ID


class ChatSend(BaseModel):
    """One user turn.

    Leave `thread_id` out to start a new conversation; the reply carries the id to reuse
    on the next turn. The backend keeps the conversation (PostgreSQL) and the LLM service
    its short-term memory, so the client never has to resend previous messages.
    """

    message: str = Field(min_length=1, max_length=4000)
    thread_id: str | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"message": "Đi Đà Lạt 3 ngày, 2 người lớn 1 bé, ngân sách 15tr?"}]
        }
    )


class ChatReply(BaseModel):
    thread_id: str
    reply: str
    trip: "TripState | None" = Field(
        default=None, description="Trip form after this turn (see `TripState`)"
    )


class ChatMessage(BaseModel):
    role: str = Field(description="human | ai")
    content: str
    plan: dict[str, Any] | None = Field(
        default=None,
        description='Trip plan built in the chat: {"form": PlanForm, "result": PlanResponse}',
    )
    created_at: datetime | None = None


class ChatHistory(BaseModel):
    thread_id: str
    messages: list[ChatMessage]


class ChatThread(BaseModel):
    thread_id: str
    title: str
    created_at: datetime
    last_active_at: datetime


class ChatRename(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class TripInfo(BaseModel):
    """Trip details mentioned so far in a conversation, to prefill the chat's trip form.

    Empty (null / []) means "not mentioned yet". Choice values (persona, access, interests,
    transport, pace) are the form's own option ids, defined by the LLM service.
    """

    persona: str | None = None
    destination: str | None = None
    start_date: str | None = None
    days: int | None = None
    adults: int | None = None
    children: int | None = None
    seniors: int | None = None
    budget_million: float | None = None
    budget_hard: bool | None = None
    access: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    transport: str | None = None
    pace: str | None = None


class MissingField(BaseModel):
    field: str = Field(description="destination | start_date | days | party | budget_million")
    label: str = Field(description='Vietnamese label, e.g. "ngày đi"')


class TripState(BaseModel):
    """The trip form of a conversation, filled from the chat by the LLM service.

    `missing` lists the required fields still empty — the assistant asks for them when the
    user wants a plan. `open_form` is true on the one turn where the form just became
    complete and the user wants a plan: the client opens the form for confirmation.
    """

    info: TripInfo = Field(default_factory=TripInfo)
    missing: list[MissingField] = Field(default_factory=list)
    wants_plan: bool = False
    open_form: bool = False


ChatReply.model_rebuild()


class ChatPlanCreate(BaseModel):
    """ "Lập lộ trình" pressed in the chat's trip form.

    `form` is the trip form of the LLM service's planning pipeline (`ailusion.itinerary.TripForm`:
    destination, start_date, end_date, group_size, budget, purposes, busy_slots...). The LLM
    service owns its schema and validation, so it is forwarded as is; a 422 upstream comes back
    as a 422. `plan_thread_id` (client-made, `plan-` + 16 hex) lets the browser follow progress
    on `GET /plan/{plan_thread_id}/events`. `trip` is the chat form as the user confirmed it.
    """

    form: dict[str, Any]
    plan_thread_id: str | None = Field(default=None, pattern=PLAN_THREAD_ID)
    trip: TripInfo | None = None


class ChatPlanReply(BaseModel):
    """The plan, plus the two messages appended to the conversation."""

    thread_id: str = Field(
        description="The conversation holding the plan — new when planning from an empty chat"
    )

    plan: dict[str, Any] = Field(
        description="The LLM service's `ItineraryResponse`, unchanged. `status` is `need_info` "
        "when questions must be answered first; nothing is saved to the conversation then."
    )
    request_text: str = Field(description="The confirmed form, as the user's message")
    reply: str = Field(description="The plan as markdown, as the assistant's message")
