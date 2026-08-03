"""PR27 — support chat configuration response schemas."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class SupportChatConfigResponse(BaseModel):
    """Public support configuration. Never includes FCM, email, or private profile."""

    model_config = ConfigDict(extra="forbid")

    available: bool
    supportUserAppId: Optional[str] = None
    displayName: str = Field(default="OpenBid Support")
    profileImageUrl: Optional[str] = None
