from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Client-generated so the browser can subscribe to progress events before the plan returns.
PLAN_THREAD_ID = r"^plan-[0-9a-f]{16}$"


class PlanCreate(BaseModel):
    """The trip-planning form (8 groups, 29 fields) plus the user's persona.

    The LLM service owns the form's full schema and validation (`ailusion.planning.request`),
    so here we only check the outer shape and forward what the client sent. Groups the
    client leaves out are not forwarded either: the LLM service fills its defaults and
    lists them as assumptions in the result.

    `thread_id` is optional. Send one (`plan-` + 16 hex chars) to open
    `GET /plan/{thread_id}/events` while the plan is being built.
    """

    persona: Literal["P1", "P2", "P3", "P4"] | None = Field(
        default=None,
        description="P1 khám phá tiết kiệm · P2 thoải mái, dễ tiếp cận · "
        "P3 thư giãn, trải nghiệm · P4 quỹ thời gian hạn chế",
    )
    basic: dict[str, Any] | None = None
    budget: dict[str, Any] | None = None
    time: dict[str, Any] | None = None
    interests: dict[str, Any] | None = None
    weather: dict[str, Any] | None = None
    transport: str | None = None
    mobility: str | None = None
    access: dict[str, Any] | None = None
    thread_id: str | None = Field(default=None, pattern=PLAN_THREAD_ID)

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "persona": "P2",
                    "basic": {
                        "destination": "Đà Lạt",
                        "start_date": "2026-10-17",
                        "days": 3,
                        "adults": 2,
                        "children": 1,
                        "seniors": 2,
                    },
                    "budget": {"total_vnd": 20000000, "hard": False},
                    "interests": {"tags": {"thiennhien": 2}},
                    "weather": {"rain": "indoor"},
                    "mobility": "nhe",
                }
            ]
        }
    )

    def form(self) -> dict[str, Any]:
        """Only the fields the client actually sent, minus our own routing fields."""
        return self.model_dump(exclude_unset=True, exclude={"thread_id"})
