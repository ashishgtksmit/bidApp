from sqlalchemy import Column, BigInteger, String
from ..database import Base

class RequestType(Base):
    # __tablename__ = 'requestTypeDetails'
    __tablename__ = 'requesttypedetails'
    RTDID = Column(BigInteger,autoincrement=True,primary_key=True)
    requestType = Column(String(250),nullable=False)