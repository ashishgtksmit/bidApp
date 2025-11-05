from sqlalchemy import Column, BigInteger, ForeignKey, DECIMAL,String,TIMESTAMP,func, Integer
from ..database import Base

class BidDetail(Base):
    # __tablename__ = 'bidDetails'
    __tablename__ = 'biddetails'

    BID = Column(BigInteger, primary_key=True, autoincrement=True)
    rID = Column(BigInteger, ForeignKey("requestTable.RID"), nullable=False)
    bidderID = Column(BigInteger, ForeignKey("userTable.userAppId"), nullable=False)
    CARID = Column(Integer, nullable=True)
    bidAmount = Column(DECIMAL(11,2),nullable=False)
    bidStatus = Column(String(100),nullable=True)
    tableTimestamp = Column(TIMESTAMP, nullable=False, server_default=func.now(),onupdate=func.now())
    last_updated = Column(TIMESTAMP, nullable=False, server_default=func.now(),onupdate=func.now())


    def __repr__(self):
        return f"<BidDetails (BID = {self.BID}, rID = {self.rID}, bidderId = {self.bidderID}, amount={self.bidAmount}, status='{self.bidStatus}')>"