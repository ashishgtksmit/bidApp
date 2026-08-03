from sqlalchemy import (
    Column,
    BigInteger,
    String,
    Text,
    TIMESTAMP,
    Numeric,
    UniqueConstraint,
    func,
)
from ..database import Base


class CustomerReview(Base):
    # __tablename__ = 'customerReviews'
    __tablename__ = "customerreviews"
    __table_args__ = (
        UniqueConstraint("RID", name="uq_customerreviews_rid"),
    )

    CR = Column(BigInteger, primary_key=True, autoincrement=True)
    RID = Column(String(20), nullable=False)
    ratingGiverUserAppId = Column(String(20), nullable=False)
    ratingReceiverUserAppId = Column(String(20), nullable=False)
    # PR19: DECIMAL(2,1) — preserves half-star ratings (0.5–5.0)
    generalRating = Column(Numeric(2, 1), nullable=False)
    comments = Column(Text, nullable=False)
    tableTimestamp = Column(TIMESTAMP, server_default=func.now())

    def __repr__(self):
        return (
            f"<CustomerReviews(CR={self.CR}, RID='{self.RID}', "
            f"rating={self.generalRating})>"
        )
