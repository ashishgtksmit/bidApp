from sqlalchemy import Column, BigInteger, String, Text, TIMESTAMP, func
from ..database import Base


class CustomerReview(Base):
    # __tablename__ = 'customerReviews'
    __tablename__ = 'customerreviews'

    CR = Column(BigInteger, primary_key=True, autoincrement=True)
    RID = Column(String(20), nullable=False)
    ratingGiverUserAppId = Column(String(20), nullable=False)
    ratingReceiverUserAppId = Column(String(20), nullable=False)
    generalRating = Column(String(20), nullable=False)
    comments = Column(Text, nullable=False)
    tableTimestamp = Column(TIMESTAMP, server_default=func.now())

    def __repr__(self):
        return f"<CustomerReviews(CR={self.CR}, RID='{self.RID}', rating={self.generalRating})>"