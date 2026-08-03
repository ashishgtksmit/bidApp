"""PR28 — chat media upload / compensation cleanup schemas."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChatMediaUploadRequest(BaseModel):
    """One PHOTO upload bound to an authorized thread + reserved messageId."""

    model_config = ConfigDict(extra="forbid")

    threadId: str = Field(..., min_length=1, max_length=128)
    messageId: str = Field(..., min_length=1, max_length=128)
    mediaType: Literal["PHOTO"] = "PHOTO"
    fileName: Optional[str] = Field(default=None, max_length=255)
    mimeType: Optional[str] = Field(default=None, max_length=64)
    content: str = Field(..., min_length=1)

    @field_validator("threadId", "messageId", "mediaType", mode="before")
    @classmethod
    def _strip_required(cls, value):
        if value is None:
            return value
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("fileName", "mimeType", mode="before")
    @classmethod
    def _strip_optional(cls, value):
        if value is None:
            return value
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned if cleaned else None
        return value


class ChatMediaUploadResponse(BaseModel):
    message: Literal["UPLOADED"]
    mediaUrl: str
    mimeType: str
    fileName: str
    sizeBytes: int


class ChatMediaCleanupRequest(BaseModel):
    """Pre-RTDB-commit compensation only. No URL/path/container from client."""

    model_config = ConfigDict(extra="forbid")

    threadId: str = Field(..., min_length=1, max_length=128)
    messageId: str = Field(..., min_length=1, max_length=128)

    @field_validator("threadId", "messageId", mode="before")
    @classmethod
    def _strip_required(cls, value):
        if value is None:
            return value
        if isinstance(value, str):
            return value.strip()
        return value


class ChatMediaCleanupResponse(BaseModel):
    message: Literal["DELETED"]
