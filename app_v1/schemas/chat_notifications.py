"""PR26 — chat push notification request/response schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChatNotificationRequest(BaseModel):
    """Client supplies only threadId + messageId. Extra fields are rejected."""

    model_config = ConfigDict(extra="forbid")

    threadId: str = Field(..., min_length=1)
    messageId: str = Field(..., min_length=1)

    @field_validator("threadId", "messageId", mode="before")
    @classmethod
    def _strip_required(cls, value):
        if value is None:
            return value
        if isinstance(value, str):
            return value.strip()
        return value


ChatNotificationOutcome = Literal[
    "NOTIFICATION_SENT",
    "NO_TOKEN",
    "NOTIFICATION_SKIPPED",
    "ALREADY_HANDLED",
]


class ChatNotificationResponse(BaseModel):
    message: ChatNotificationOutcome
