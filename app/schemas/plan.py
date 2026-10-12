from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Client-generated so the browser can subscribe to progress events before the plan returns.
PLAN_THREAD_ID = r"^plan-[0-9a-f]{16}$"


class PlanCreate(BaseModel):
    """The trip form of the LLM service's itinerary pipeline (`TripForm`).

    The LLM service owns the form's schema and validation (`ailusion.itinerary.form`), so here
    we only check the routing field and forward the rest unchanged. Send only the fields the
    user filled in: the LLM service reads blank ones from `message` or asks a follow-up
    question (`status: "need_info"`).

    `thread_id` is optional. Send one (`plan-` + 16 hex chars) to open
    `GET /plan/{thread_id}/events` while the plan is being built.
    """

    thread_id: str | None = Field(default=None, pattern=PLAN_THREAD_ID)

    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "examples": [
                {
                    "destination": "Đà Nẵng",
                    "start_date": "2026-10-25",
                    "end_date": "2026-10-27",
                    "group_size": 2,
                    "group_type": "couple",
                    "budget": 15000000,
                    "purposes": ["relax", "food"],
                }
            ]
        },
    )

    def form(self) -> dict[str, Any]:
        """The fields the client sent, minus our own routing field."""
        return self.model_dump(exclude_unset=True, exclude={"thread_id"})
