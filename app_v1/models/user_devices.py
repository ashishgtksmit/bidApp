"""PR64 — install device bindings for related-party matching."""

from sqlalchemy import (
    Column,
    Index,
    Integer,
    String,
    TIMESTAMP,
    UniqueConstraint,
    func,
)

from ..database import Base


class UserDevice(Base):
    __tablename__ = "user_devices"
    __table_args__ = (
        UniqueConstraint("userAppId", "deviceId", name="uq_user_devices_user_device"),
        Index("ix_user_devices_deviceId", "deviceId"),
    )

    # Integer (not BigInteger) so SQLite autoincrement works in unit tests.
    id = Column(Integer, primary_key=True, autoincrement=True)
    userAppId = Column(String(64), nullable=False)
    deviceId = Column(String(128), nullable=False)
    lastSeenAt = Column(
        TIMESTAMP,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
    createdAt = Column(
        TIMESTAMP,
        nullable=False,
        server_default=func.current_timestamp(),
    )

    def __repr__(self) -> str:
        return (
            f"<UserDevice(id={self.id}, userAppId='{self.userAppId}', "
            f"deviceId='{self.deviceId}')>"
        )
