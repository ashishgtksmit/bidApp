from sqlalchemy import (
    Column,
    BigInteger,
    String,
    Boolean,
    TIMESTAMP,
    Text,
    Numeric,
    UniqueConstraint,
    func,
)
from ..database import Base


class VendorReview(Base):
    # __tablename__ = 'vendorReviews'
    __tablename__ = "vendorreviews"
    __table_args__ = (
        UniqueConstraint("RID", name="uq_vendorreviews_rid"),
    )

    VRID = Column(BigInteger, autoincrement=True, primary_key=True)
    customerAppId = Column(String(10), nullable=False)
    RID = Column(BigInteger, nullable=False)
    VENDORID = Column(BigInteger, nullable=False)
    # PR19: DECIMAL(2,1) — preserves half-star ratings (0.5–5.0)
    driverBehaviour = Column(Numeric(2, 1), nullable=False)
    punctuality = Column(Numeric(2, 1), nullable=False)
    carCondition = Column(Numeric(2, 1), nullable=False)
    cleanliness = Column(Numeric(2, 1), nullable=False)
    refreshments = Column(Boolean, nullable=False)
    comments = Column(Text, nullable=False)
    tableTimestamp = Column(
        TIMESTAMP,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self):
        return (
            f"<VendorReviews(VRID={self.VRID}, RID={self.RID}, "
            f"VENDORID={self.VENDORID}, driverBehaviour={self.driverBehaviour})>"
        )
