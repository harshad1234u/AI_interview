import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from app.models.interview_session import FinishReason, InterviewStatus
from app.schemas.interview_plan import PlanSettings, PlanSource
from app.services.interview_state_machine import ClientAction


class InterviewSessionCreate(BaseModel):
    """The session is for the signed-in candidate (Phase 12). A candidate_id
    sent by the client is refused (422), never trusted. abandon_active allows
    atomically abandoning any active session to start a new one (AUDIT-008)."""

    model_config = ConfigDict(extra="forbid")
    abandon_active: bool = False


class RetryRequest(BaseModel):
    """Empty on purpose: the retried topics are decided by code from the report (Phase 10)."""

    model_config = ConfigDict(extra="forbid")


class SessionActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Client actions only: system events (PLAN_READY, FINISH, ...) fail validation with 422.
    action: ClientAction


class InterviewSessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    candidate_id: uuid.UUID
    status: InterviewStatus
    created_at: datetime
    updated_at: datetime
    finish_reason: FinishReason | None
    # For the candidate's history: the interview this one retries (NULL for a first attempt, or
    # once that interview was deleted), and what was planned (NULL before planning).
    retry_of_id: uuid.UUID | None
    settings: PlanSettings | None = None
    plan_source: PlanSource | None = None

    @model_validator(mode="before")
    @classmethod
    def plan_summary(cls, data: Any) -> Any:
        plan = None if isinstance(data, dict) else getattr(data, "plan", None)
        if plan is None:
            return data
        fields = ("id", "candidate_id", "status", "created_at", "updated_at", "finish_reason",
                  "retry_of_id")
        return {**{f: getattr(data, f) for f in fields},
                "settings": plan["settings"], "plan_source": plan["source"]}
