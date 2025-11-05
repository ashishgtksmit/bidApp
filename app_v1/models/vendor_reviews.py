from sqlalchemy import Column, BigInteger, String, Integer, Boolean, TIMESTAMP, Text, func
from ..database import Base


class VendorReview(Base):
    # __tablename__ = 'vendorReviews'
    __tablename__ = 'vendorreviews'

    VRID = Column(BigInteger, autoincrement=True, primary_key=True)
    customerAppId = Column(String(10), nullable=False)
    RID = Column(BigInteger, nullable=False)
    VENDORID = Column(BigInteger, nullable=False)
    driverBehaviour = Column(Integer, nullable=False)
    punctuality = Column(Integer, nullable=False)
    carCondition = Column(Integer, nullable=False)
    cleanliness = Column(Integer, nullable=False)
    refreshments = Column(Boolean, nullable=False)
    comments = Column(Text, nullable=False)
    tableTimestamp = Column(TIMESTAMP, nullable=False, server_default=func.now(), onupdate=func.now())


    def __repr__(self):
        return f"<VendorReviews(VRID={self.VRID}, RID={self.RID}, VENDORID={self.VENDORID}, driverBehaviour={self.driverBehaviour})>"