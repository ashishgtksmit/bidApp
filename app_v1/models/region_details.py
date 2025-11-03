from sqlalchemy import Column, BigInteger, String
from ..database import Base

class Region(Base):
    __tablename__ = 'regionDetails'
    RDID = Column(BigInteger, autoincrement=True, primary_key=True)
    regionName = Column(String(300),nullable=False)