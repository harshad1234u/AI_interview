import uuid
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    EmailStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.models.candidate import NAME_MAX_LENGTH

Name = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX_LENGTH),
    AfterValidator(lambda v: " ".join(v.split())),
]
# email-validator already enforces the 254-char limit (EMAIL_MAX_LENGTH) and trims whitespace.
Email = Annotated[EmailStr, AfterValidator(str.lower)]


# Length only: no composition rules (NIST SP 800-63B). The upper bound caps hashing work.
PASSWORD_MIN_LENGTH, PASSWORD_MAX_LENGTH = 8, 128
NewPassword = Annotated[
    str, StringConstraints(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)
]


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Email
    name: Name
    password: NewPassword


class RegisterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    verification_required: bool = True
    message: str = "Verification email sent. Please check your inbox to activate your account."


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Email
    # Any length policy is for new passwords; here only the hashing bound applies.
    password: Annotated[str, StringConstraints(min_length=1, max_length=PASSWORD_MAX_LENGTH)]


class ForgotPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Email


class ResetPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_token: str
    new_password: NewPassword


class MessageResponse(BaseModel):
    message: str


class PartialUpdate(BaseModel):
    """PATCH body: every field optional, none may be null, at least one must be sent."""

    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def reject_explicit_null(cls, v: Any) -> Any:
        if v is None:
            raise ValueError("may be omitted but not null")
        return v

    @model_validator(mode="after")
    def require_a_field(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("provide at least one field to update")
        return self


class CandidateUpdate(PartialUpdate):
    email: Email | None = None
    name: Name | None = None


class CandidateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    name: str
    created_at: datetime
