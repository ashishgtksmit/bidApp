from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    Column,
    Date,
    ForeignKey,
    Integer,
    String,
    Time,
    func,
)

from ..database import Base


class SightseeingSpot(Base):
    __tablename__ = "ob_sightseeing_spots"

    spotId = Column(BigInteger, primary_key=True, autoincrement=True)
    locationId = Column(BigInteger, nullable=False)
    spotCode = Column(String(64), nullable=False, unique=True)
    spotName = Column(String(200), nullable=False)
    permitRequired = Column(Boolean, nullable=False, default=False)
    typicalDurationMins = Column(Integer, nullable=True)
    isActive = Column(Boolean, nullable=False, default=True)
    sortOrder = Column(Integer, nullable=False, default=0)
    createdAt = Column(TIMESTAMP, server_default=func.current_timestamp())
    updatedAt = Column(TIMESTAMP, server_default=func.current_timestamp())


class RequestItineraryDay(Base):
    __tablename__ = "request_itinerary_days"

    dayId = Column(BigInteger, primary_key=True, autoincrement=True)
    requestId = Column(BigInteger, nullable=False)
    dayNo = Column(Integer, nullable=False)
    travelDate = Column(Date, nullable=False)
    startTime = Column(Time, nullable=False)
    pickupLandmark = Column(String(200), nullable=False)
    createdAt = Column(TIMESTAMP, server_default=func.current_timestamp())


class RequestItineraryDaySpot(Base):
    __tablename__ = "request_itinerary_day_spots"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    dayId = Column(
        BigInteger,
        ForeignKey("request_itinerary_days.dayId", ondelete="CASCADE"),
        nullable=False,
    )
    spotId = Column(BigInteger, ForeignKey("ob_sightseeing_spots.spotId"), nullable=False)
    sequenceNo = Column(Integer, nullable=False)
    spotNameSnapshot = Column(String(200), nullable=False)
    permitRequiredSnapshot = Column(Boolean, nullable=False, default=False)
