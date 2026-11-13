from sqlalchemy import DECIMAL, TIMESTAMP, BigInteger, Column, Integer, func

from ..database import Base


class BidDayPrice(Base):
    __tablename__ = "bid_day_prices"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    bidId = Column(BigInteger, nullable=False)
    dayNo = Column(Integer, nullable=False)
    amount = Column(DECIMAL(11, 2), nullable=False)
    createdAt = Column(TIMESTAMP, server_default=func.current_timestamp())
