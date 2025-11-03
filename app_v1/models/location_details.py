from sqlalchemy import Column,BigInteger,String,Integer
from ..database import Base

class LocationDetail(Base):
    
    __tablename__ = 'location_details'

    LID = Column(BigInteger, autoincrement=True, primary_key=True)
    location = Column(String(50),nullable=False)    
    location_shortCode = Column(String(10),nullable=True)
    regionId = Column(Integer,nullable=False)